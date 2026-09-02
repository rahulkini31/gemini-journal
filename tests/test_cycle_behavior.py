"""End-to-end behavior contracts for the deterministic cycle boundary."""
from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound import cycle  # noqa: E402
from bookbound.agents import Decision  # noqa: E402
from bookbound.book import Book  # noqa: E402
from bookbound.capture import load_capture  # noqa: E402
from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.execute import OrderResult  # noqa: E402
from bookbound.exits import ExitLeg, ExitPlan  # noqa: E402
from bookbound.market import (  # noqa: E402
    Contract,
    OptionDeliverable,
    UnderlyingSnapshot,
)
from bookbound.structures import Structure  # noqa: E402


AS_OF = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
EXPIRY = date(2026, 9, 25)
SETTINGS = Settings(
    api_key="k", secret_key="s", universe=("SPY",), risk=RiskBudget(),
)
UNDERLYING = UnderlyingSnapshot("SPY", 102.0, AS_OF, "latest_trade")


def contract(
    symbol: str, strike: float, delta: float, *, bid: float, ask: float,
    timestamp: datetime = AS_OF,
) -> Contract:
    return Contract(
        symbol=symbol, underlying="SPY", expiry=EXPIRY, kind="C",
        strike=strike, bid=bid, ask=ask, delta=delta, gamma=.01,
        theta=-.20, vega=.10, iv=.20, quote_timestamp=timestamp,
        bid_size=10, ask_size=10,
        # Entry candidates must be matched to the contract master; an
        # unverified quote is refused before any economics are considered.
        multiplier=100, contract_size=100, contract_status="active",
        contract_tradable=True, contract_style="american", root_symbol="SPY",
        deliverables=(OptionDeliverable(
            type="equity", symbol="SPY", amount=100.0,
            allocation_percentage=100.0,
        ),),
        metadata_verified=True,
    )


def structure(label: str = "A", *, gap: float = .02) -> Structure:
    long = contract(f"SPY260925C00100{label:0>3}", 100, .50, bid=4.95, ask=5.05)
    short = contract(
        f"SPY260925C00105{label:0>3}", 105, .30,
        bid=3.00 - gap, ask=3.10 - gap,
    )
    return Structure(
        name="bull_call_spread", underlying="SPY", expiry=EXPIRY, qty=1,
        long=long, short=short, net_debit=2.0, max_loss=200,
        max_gain=300, spot=102, realised_vol=.18, forward=102.1,
        valuation_date=date(2026, 9, 2),
    )


def empty_book(*, positions=None, orders=None) -> Book:
    return Book(
        equity=100_000, last_equity=100_000, cash=100_000,
        buying_power=400_000, options_buying_power=100_000, legs=[],
        raw_positions=positions or [], raw_orders=orders or [], spots={"SPY": 102},
    )


class TestCycleClock(unittest.TestCase):
    def test_given_broker_clock_then_one_aware_as_of_is_resolved(self):
        clock = {"timestamp": "2026-09-02T11:00:00-04:00"}
        self.assertEqual(cycle.resolve_cycle_as_of(clock), AS_OF)

    def test_given_open_clock_without_valid_timestamp_then_cycle_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "market clock timestamp"):
            cycle.resolve_cycle_as_of({"is_open": True, "timestamp": "bad"})


class TestCandidateFunnelOrder(unittest.TestCase):
    def test_given_gated_leader_then_it_cannot_consume_the_top_k_slot(self):
        bad = structure("1", gap=.001)
        good = structure("2", gap=.20)
        settings = dataclasses.replace(
            SETTINGS,
            risk=dataclasses.replace(SETTINGS.risk, shortlist_size=1),
        )

        def verdict(item, *_args, **_kwargs):
            return SimpleNamespace(admitted=item.key == good.key, reasons=[])

        with (
            patch.object(cycle, "build_vertical_debit_spreads",
                         side_effect=[[bad, good], []]) as debit,
            patch.object(cycle, "build_vertical_credit_spreads",
                         side_effect=[[], []]) as credit,
            patch.object(cycle, "evaluate", side_effect=verdict),
        ):
            funnel = cycle.construct_candidate_funnel(
                [], {"SPY": 102}, {"SPY": .18}, empty_book(), settings,
                unpriced_positions=[], as_of=AS_OF,
            )

        self.assertEqual([item.key for item in funnel.shortlist], [good.key])
        self.assertEqual(len(funnel.built), 2)
        self.assertEqual(len(funnel.admissible), 1)
        for call in [*debit.call_args_list, *credit.call_args_list]:
            self.assertIsNone(call.kwargs["max_candidates"])
            self.assertIs(call.kwargs["as_of"], AS_OF)
            self.assertEqual(call.kwargs["spots"], {"SPY": 102})


class TestPreSubmitRevalidation(unittest.TestCase):
    def test_given_single_leg_working_order_then_refresh_set_includes_it(self):
        original = structure()
        pending_symbol = "SPY260925P00095000"
        book = empty_book(orders=[{
            "status": "new", "symbol": pending_symbol,
            "position_intent": "buy_to_open",
        }])

        symbols = cycle._option_symbols_for_refresh(book, original)

        self.assertIn(pending_symbol, symbols)

    def test_given_stale_refreshed_leg_then_revalidation_refuses(self):
        original = structure()
        stale = AS_OF - timedelta(minutes=5)
        refreshed = {
            original.long.symbol: dataclasses.replace(
                original.long, quote_timestamp=stale,
            ),
            original.short.symbol: dataclasses.replace(
                original.short, quote_timestamp=stale,
            ),
        }

        result = cycle.validate_refreshed_structure(
            original, refreshed, 102, empty_book(), SETTINGS,
            unpriced_positions=[], as_of=AS_OF,
        )

        self.assertFalse(result.admitted)
        self.assertIn("stale", result.reason.lower())

    def test_given_price_deteriorates_beyond_policy_then_revalidation_refuses(self):
        original = structure()
        refreshed = {
            original.long.symbol: dataclasses.replace(
                original.long, bid=5.70, ask=5.80,
            ),
            original.short.symbol: original.short,
        }

        result = cycle.validate_refreshed_structure(
            original, refreshed, 102, empty_book(), SETTINGS,
            unpriced_positions=[], as_of=AS_OF,
        )

        self.assertFalse(result.admitted)
        self.assertIn("deterioration", result.reason.lower())


class TestCycleCaptureIntegration(unittest.TestCase):
    def test_given_no_trade_cycle_then_raw_inputs_are_sealed_for_replay(self):
        book = empty_book(orders=[{"id": "working-1", "status": "new"}])
        context = {
            "as_of": AS_OF.isoformat(),
            "underlyings": {
                "SPY": {"available": True, "realised_vol_annualised": .18},
            },
        }

        def underlier(_settings, symbol, *, raw_sink=None):
            if raw_sink is not None:
                raw_sink(symbol, {
                    "latestTrade": {"p": 102, "t": AS_OF.isoformat()},
                })
            return UNDERLYING

        def chain(_settings, symbol, **kwargs):
            kwargs["raw_sink"](symbol, {"UNUSABLE_RAW_ROW": {"greeks": None}})
            return []

        with tempfile.TemporaryDirectory() as directory:
            settings = dataclasses.replace(
                SETTINGS, capture_root=Path(directory),
            )
            with (
                patch.object(cycle, "market_clock", return_value={
                    "is_open": True, "timestamp": AS_OF.isoformat(),
                }),
                patch.object(cycle, "underlying_snapshot", side_effect=underlier),
                patch.object(cycle, "option_chain", side_effect=chain),
                patch.object(cycle, "load_book", return_value=book),
                patch.object(cycle, "_price_for_close",
                             new=lambda plan, settings, *, as_of: plan),
                patch.object(cycle, "evaluate_structure_exits", return_value=[]),
                patch.object(cycle.market_context, "build", return_value=context),
                patch.object(cycle.AuditLog, "write", return_value={}),
            ):
                report = cycle.run_cycle(settings, dry_run=True)

            self.assertEqual(report.outcome, "NO_TRADE")
            self.assertIsNotNone(report.capture_path)
            replay = load_capture(Path(report.capture_path))
            self.assertEqual(replay.chain_pages[0]["underlying"], "SPY")
            self.assertIn("UNUSABLE_RAW_ROW", replay.chain_pages[0]["snapshots"])
            self.assertEqual(replay.underlier_snapshots["SPY"]["latestTrade"]["p"], 102)
            self.assertEqual(
                replay.book["snapshots"][0]["book"]["equity"], 100_000,
            )
            self.assertEqual(
                replay.working_orders[0]["orders"][0]["id"], "working-1",
            )

    def test_given_capture_cannot_seal_then_new_entry_is_refused(self):
        selected = structure("CAPTURE")
        book = empty_book()
        refreshed = {
            selected.long.symbol: selected.long,
            selected.short.symbol: selected.short,
        }
        decision = Decision(
            selected, {"thesis": "specific"}, {"reason": "approved"},
            "SELECTED", "specific",
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = dataclasses.replace(
                SETTINGS, capture_root=Path(directory),
            )
            with (
                patch.object(cycle, "market_clock", return_value={
                    "is_open": True, "timestamp": AS_OF.isoformat(),
                }),
                patch.object(cycle, "underlying_snapshot", return_value=UNDERLYING),
                patch.object(cycle, "option_chain", return_value=[]),
                patch.object(cycle, "option_snapshots", return_value=refreshed),
                patch.object(cycle, "load_book", return_value=book),
                patch.object(cycle, "_price_for_close",
                             new=lambda plan, settings, *, as_of: plan),
                patch.object(cycle, "evaluate_structure_exits", return_value=[]),
                patch.object(cycle, "construct_candidate_funnel", return_value=
                             cycle.CandidateFunnel(
                                 (selected,), (selected,), (selected,),
                             )),
                patch.object(cycle.market_context, "build", return_value={
                    "underlyings": {
                        "SPY": {"available": True,
                                "realised_vol_annualised": .18},
                    },
                }),
                patch.object(cycle, "decide", return_value=decision),
                patch.object(cycle, "shrink_to_fit", return_value=selected),
                patch.object(cycle, "evaluate", return_value=cycle.Verdict(True)),
                patch.object(cycle.DecisionCapture, "seal",
                             side_effect=OSError("disk full")),
                patch.object(cycle.execute, "submit") as submit,
                patch.object(cycle.AuditLog, "write", return_value={}),
            ):
                report = cycle.run_cycle(settings, dry_run=True)

        self.assertEqual(report.outcome, "CAPTURE_FAILED")
        self.assertIn("disk full", report.capture_error)
        submit.assert_not_called()


class TestCycleSafetyStops(unittest.TestCase):
    def _base_patches(self, book: Book):
        return (
            patch.object(cycle, "market_clock", return_value={
                "is_open": True, "timestamp": AS_OF.isoformat(),
            }),
            patch.object(cycle, "underlying_snapshot", return_value=UNDERLYING),
            patch.object(cycle, "option_chain", return_value=[]),
            patch.object(cycle, "load_book", return_value=book),
                patch.object(cycle, "_price_for_close",
                             new=lambda plan, settings, *, as_of: plan),
            patch.object(cycle.AuditLog, "write", return_value={}),
        )

    def test_given_reconciliation_breach_then_repair_is_atomic_and_entries_halt(self):
        positions = [{
            "asset_class": "us_option", "symbol": "SPY260925C00105000",
            "qty": "1", "side": "short", "cost_basis": "200",
            "unrealized_pl": "0",
        }]
        book = empty_book(positions=positions)
        result = OrderResult(True, None, "repair", "DRY_RUN", {})

        patches = self._base_patches(book)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(cycle.execute, "close_structure", return_value=result) as close, \
             patch.object(cycle, "decide") as decide:
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "RECONCILIATION_REQUIRED")
        self.assertEqual(len(report.repairs), 1)
        self.assertEqual(len(close.call_args.args[0].legs), 1)
        decide.assert_not_called()

    def test_given_market_data_outage_then_reconciliation_still_runs_first(self):
        positions = [{
            "asset_class": "us_option", "symbol": "SPY260925C00105000",
            "qty": "1", "side": "short", "cost_basis": "200",
            "unrealized_pl": "0",
        }]
        book = empty_book(positions=positions)
        result = OrderResult(True, None, "repair", "DRY_RUN", {})

        with (
            patch.object(cycle, "market_clock", return_value={
                "is_open": True, "timestamp": AS_OF.isoformat(),
            }),
            patch.object(cycle, "load_book", return_value=book),
                patch.object(cycle, "_price_for_close",
                             new=lambda plan, settings, *, as_of: plan),
            patch.object(
                cycle, "underlying_snapshot",
                side_effect=AssertionError("market data must follow lifecycle repair"),
            ),
            patch.object(cycle.execute, "close_structure", return_value=result) as close,
            patch.object(cycle.AuditLog, "write", return_value={}),
        ):
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "RECONCILIATION_REQUIRED")
        close.assert_called_once()

    def test_given_probable_assignment_then_entries_halt_without_guessing_repair(self):
        positions = [
            {"asset_class": "us_equity", "symbol": "SPY", "qty": "100",
             "side": "long"},
            {"asset_class": "us_option", "symbol": "SPY260925C00105000",
             "qty": "1", "side": "long", "cost_basis": "200",
             "unrealized_pl": "0"},
        ]
        book = empty_book(positions=positions)

        with (
            patch.object(cycle, "market_clock", return_value={
                "is_open": True, "timestamp": AS_OF.isoformat(),
            }),
            patch.object(cycle, "load_book", return_value=book),
                patch.object(cycle, "_price_for_close",
                             new=lambda plan, settings, *, as_of: plan),
            patch.object(cycle, "underlying_snapshot") as price,
            patch.object(cycle.execute, "close_structure") as close,
            patch.object(cycle.AuditLog, "write", return_value={}),
        ):
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "RECONCILIATION_UNSUPPORTED")
        price.assert_not_called()
        close.assert_not_called()

    def test_given_partial_fill_and_live_opening_order_then_cancel_precedes_repair(self):
        positions = [{
            "asset_class": "us_option", "symbol": "SPY260925C00105000",
            "qty": "1", "side": "short", "cost_basis": "200",
            "unrealized_pl": "0",
        }]
        working = {
            "id": "entry-1", "status": "partially_filled",
            "created_at": (AS_OF - timedelta(seconds=30)).isoformat(),
            "legs": [
                {"symbol": "SPY260925C00100000",
                 "position_intent": "buy_to_open"},
                {"symbol": "SPY260925C00105000",
                 "position_intent": "sell_to_open"},
            ],
        }
        book = empty_book(positions=positions, orders=[working])
        cancel_result = SimpleNamespace(
            ok=True, order_id="entry-1", status="DRY_RUN", error=None,
        )
        patches = self._base_patches(book)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(cycle.execute, "cancel_order",
                          return_value=cancel_result) as cancel, \
             patch.object(cycle.execute, "close_structure") as close:
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "RECONCILIATION_PENDING_CANCEL")
        cancel.assert_called_once_with(
            "entry-1", dry_run=True, settings=SETTINGS,
        )
        close.assert_not_called()

    def test_given_structure_exit_then_one_mleg_is_sent_and_entries_halt(self):
        book = empty_book()
        plan = ExitPlan(
            "SPY", EXPIRY, "C",
            (
                ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
            ),
            "profit_target", "spread reached target",
        )
        result = OrderResult(True, None, "exit", "DRY_RUN", {})
        patches = self._base_patches(book)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(cycle, "evaluate_structure_exits", return_value=[plan]), \
             patch.object(cycle.execute, "close_structure", return_value=result) as close, \
             patch.object(cycle, "decide") as decide:
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "EXIT_REQUIRED")
        self.assertEqual(len(report.exits), 1)
        self.assertEqual(len(close.call_args.args[0].legs), 2)
        decide.assert_not_called()

    def test_given_live_opening_order_and_exit_then_parent_is_canceled_first(self):
        working = {
            "id": "entry-1", "status": "partially_filled", "qty": "2",
            "filled_qty": "1",
            "created_at": (AS_OF - timedelta(seconds=20)).isoformat(),
            "legs": [
                {"symbol": "SPY260925C00100000", "side": "buy",
                 "position_intent": "buy_to_open"},
                {"symbol": "SPY260925C00105000", "side": "sell",
                 "position_intent": "sell_to_open"},
            ],
        }
        book = empty_book(orders=[working])
        plan = ExitPlan(
            "SPY", EXPIRY, "C",
            (
                ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
            ),
            "stop_loss", "spread reached stop",
        )
        cancel_result = SimpleNamespace(
            ok=True, order_id="entry-1", status="DRY_RUN", error=None,
        )
        patches = self._base_patches(book)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(cycle, "evaluate_structure_exits", return_value=[plan]), \
             patch.object(cycle.execute, "cancel_order",
                          return_value=cancel_result) as cancel, \
             patch.object(cycle.execute, "close_structure") as close:
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "EXIT_PENDING_CANCEL")
        cancel.assert_called_once()
        close.assert_not_called()

    def test_given_matching_close_already_working_then_exit_is_not_resubmitted(self):
        closing = {
            "id": "close-1", "status": "accepted",
            "qty": "1", "filled_qty": "0",
            "created_at": (AS_OF - timedelta(seconds=10)).isoformat(),
            "legs": [
                {"symbol": "SPY260925C00100000",
                 "side": "sell", "ratio_qty": "1",
                 "position_intent": "sell_to_close"},
                {"symbol": "SPY260925C00105000",
                 "side": "buy", "ratio_qty": "1",
                 "position_intent": "buy_to_close"},
            ],
        }
        book = empty_book(orders=[closing])
        plan = ExitPlan(
            "SPY", EXPIRY, "C",
            (
                ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
            ),
            "profit_target", "spread reached target",
        )
        patches = self._base_patches(book)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(cycle, "evaluate_structure_exits", return_value=[plan]), \
             patch.object(cycle.execute, "close_structure") as close:
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "EXIT_PENDING")
        close.assert_not_called()

    def test_given_smaller_working_close_then_larger_plan_is_not_suppressed(self):
        plan = ExitPlan(
            "SPY", EXPIRY, "C",
            (
                ExitLeg("SPY260925C00100000", 3, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 3, "buy", "buy_to_close"),
            ),
            "stop_loss", "three spreads",
        )
        order = {
            "id": "close-one", "status": "accepted", "qty": "1",
            "filled_qty": "0",
            "legs": [
                {"symbol": plan.legs[0].symbol, "side": "sell",
                 "ratio_qty": "1", "position_intent": "sell_to_close"},
                {"symbol": plan.legs[1].symbol, "side": "buy",
                 "ratio_qty": "1", "position_intent": "buy_to_close"},
            ],
        }

        self.assertIsNone(cycle._matching_working_close([order], plan))
        order["qty"] = "3"
        self.assertIs(cycle._matching_working_close([order], plan), order)

    def test_given_stale_working_entry_then_cancel_occurs_before_new_decision(self):
        old = {
            "id": "old-order", "status": "new",
            "created_at": (AS_OF - timedelta(minutes=10)).isoformat(),
            "legs": [{"position_intent": "buy_to_open"}],
        }
        book = empty_book(orders=[old])
        cancel_result = SimpleNamespace(
            ok=True, order_id="old-order", status="DRY_RUN", error=None,
        )
        patches = self._base_patches(book)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(cycle.execute, "cancel_order",
                          return_value=cancel_result) as cancel, \
             patch.object(cycle, "decide") as decide:
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "STALE_ORDERS")
        cancel.assert_called_once_with(
            "old-order", dry_run=True, settings=SETTINGS,
        )
        decide.assert_not_called()

    def test_given_stop_triggers_during_market_observation_then_entry_halts(self):
        plan = ExitPlan(
            "SPY", EXPIRY, "C",
            (
                ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
            ),
            "stop_loss", "stop triggered while option pages were loading",
        )
        book = empty_book()
        result = OrderResult(True, None, "exit", "DRY_RUN", {})

        with (
            patch.object(cycle, "market_clock", return_value={
                "is_open": True, "timestamp": AS_OF.isoformat(),
            }),
            patch.object(cycle, "underlying_snapshot", return_value=UNDERLYING),
            patch.object(cycle, "option_chain", return_value=[]),
            patch.object(cycle, "load_book", side_effect=[book, book, book]),
                patch.object(cycle, "_price_for_close",
                             new=lambda plan, settings, *, as_of: plan),
            patch.object(cycle, "evaluate_structure_exits",
                         side_effect=[[], [plan]]),
            patch.object(cycle.execute, "close_structure",
                         return_value=result) as close,
            patch.object(cycle, "decide") as decide,
            patch.object(cycle.AuditLog, "write", return_value={}),
        ):
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "EXIT_REQUIRED")
        close.assert_called_once_with(plan, dry_run=True, settings=SETTINGS)
        decide.assert_not_called()

    def test_given_stop_triggers_during_model_latency_then_submit_is_refused(self):
        selected = structure("7")
        plan = ExitPlan(
            "SPY", EXPIRY, "C",
            (
                ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
                ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
            ),
            "stop_loss", "stop triggered while model was reviewing",
        )
        book = empty_book()
        result = OrderResult(True, None, "exit", "DRY_RUN", {})
        refreshed = {
            selected.long.symbol: selected.long,
            selected.short.symbol: selected.short,
        }
        decision = Decision(
            selected, {"thesis": "specific"}, {"reason": "approved"},
            "SELECTED", "specific",
        )

        with (
            patch.object(cycle, "market_clock", side_effect=[
                {"is_open": True, "timestamp": AS_OF.isoformat()},
                {"is_open": True, "timestamp": AS_OF.isoformat()},
                {"is_open": True, "timestamp": AS_OF.isoformat()},
                {"is_open": True, "timestamp": AS_OF.isoformat()},
            ]),
            patch.object(cycle, "underlying_snapshot", return_value=UNDERLYING),
            patch.object(cycle, "option_chain", return_value=[]),
            patch.object(cycle, "option_snapshots", return_value=refreshed),
            patch.object(cycle, "load_book",
                         side_effect=[book, book, book, book]),
            patch.object(cycle, "_price_for_close",
                         new=lambda plan, settings, *, as_of: plan),
            patch.object(cycle, "evaluate_structure_exits",
                         side_effect=[[], [], [plan]]),
            patch.object(cycle, "construct_candidate_funnel", return_value=
                         cycle.CandidateFunnel(
                             (selected,), (selected,), (selected,),
                         )),
            patch.object(cycle.market_context, "build", return_value={
                "underlyings": {
                    "SPY": {"available": True,
                            "realised_vol_annualised": .18},
                },
            }),
            patch.object(cycle, "decide", return_value=decision),
            patch.object(cycle, "shrink_to_fit", return_value=selected),
            patch.object(cycle.execute, "close_structure",
                         return_value=result) as close,
            patch.object(cycle.execute, "submit") as submit,
            patch.object(cycle.AuditLog, "write", return_value={}),
        ):
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "EXIT_REQUIRED")
        close.assert_called_once_with(plan, dry_run=True, settings=SETTINGS)
        submit.assert_not_called()

    def test_given_refresh_latency_ages_quotes_then_submit_is_refused(self):
        selected = structure("8")
        book = empty_book()
        refreshed = {
            selected.long.symbol: selected.long,
            selected.short.symbol: selected.short,
        }
        decision = Decision(
            selected, {"thesis": "specific"}, {"reason": "approved"},
            "SELECTED", "specific",
        )
        late = AS_OF + timedelta(seconds=121)

        with (
            patch.object(cycle, "market_clock", side_effect=[
                {"is_open": True, "timestamp": AS_OF.isoformat()},
                {"is_open": True, "timestamp": AS_OF.isoformat()},
                {"is_open": True, "timestamp": AS_OF.isoformat()},
                {"is_open": True, "timestamp": late.isoformat()},
            ]),
            patch.object(cycle, "underlying_snapshot", return_value=UNDERLYING),
            patch.object(cycle, "option_chain", return_value=[]),
            patch.object(cycle, "option_snapshots", return_value=refreshed),
            patch.object(cycle, "load_book", return_value=book),
                patch.object(cycle, "_price_for_close",
                             new=lambda plan, settings, *, as_of: plan),
            patch.object(cycle, "evaluate_structure_exits", return_value=[]),
            patch.object(cycle, "construct_candidate_funnel", return_value=
                         cycle.CandidateFunnel(
                             (selected,), (selected,), (selected,),
                         )),
            patch.object(cycle.market_context, "build", return_value={
                "underlyings": {
                    "SPY": {"available": True,
                            "realised_vol_annualised": .18},
                },
            }),
            patch.object(cycle, "decide", return_value=decision),
            patch.object(cycle, "shrink_to_fit", return_value=selected),
            patch.object(cycle, "evaluate", return_value=cycle.Verdict(True)),
            patch.object(cycle.execute, "submit",
                         return_value=OrderResult(
                             True, None, "entry", "DRY_RUN", {},
                         )) as submit,
            patch.object(cycle.AuditLog, "write", return_value={}),
        ):
            report = cycle.run_cycle(SETTINGS, dry_run=True)

        self.assertEqual(report.outcome, "REPRICE_REJECTED")
        self.assertIn("stale", report.detail.lower())
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
