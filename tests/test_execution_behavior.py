"""Behavior specifications for idempotent, bounded order lifecycle."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound import execute  # noqa: E402
from bookbound.config import Settings  # noqa: E402
from bookbound.market import Contract  # noqa: E402
from bookbound.exits import ExitLeg, ExitPlan, price_exit_plan  # noqa: E402
from bookbound.structures import _price_debit_spread  # noqa: E402


EXPIRY = date(2026, 9, 25)
SETTINGS = Settings(api_key="read-write-key", secret_key="secret", paper=True)
AS_OF = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)


def structure(width: float = 5.0):
    long = Contract(
        "SPY260925C00100000", "SPY", EXPIRY, "C", 100, 4.9, 5.0,
        .50, .02, -.3, .1, .2,
    )
    short = Contract(
        f"SPY260925C{int((100 + width) * 1000):08d}", "SPY", EXPIRY, "C",
        100 + width, 2.9, 3.0, .30, .015, -.2, .08, .2,
    )
    built = _price_debit_spread(
        long, short, "C", EXPIRY, spot=102, realised_vol=.18,
        forward=102.1, valuation_date=date(2026, 9, 2),
    )
    assert built is not None
    return built


def exit_quote(
    symbol: str,
    *,
    bid: float,
    ask: float,
    timestamp: datetime = AS_OF - timedelta(seconds=5),
    bid_size: int = 10,
    ask_size: int = 10,
) -> Contract:
    parsed = execute.parse_occ(symbol)
    assert parsed is not None
    underlying, expiry, kind, strike = parsed
    return Contract(
        symbol, underlying, expiry, kind, strike, bid, ask,
        .25 if kind == "C" else -.25, .01, -.05, .10, .20,
        quote_timestamp=timestamp,
        bid_size=bid_size,
        ask_size=ask_size,
    )


def vertical_exit_plan() -> ExitPlan:
    return ExitPlan(
        "SPY", EXPIRY, "C",
        (
            ExitLeg("SPY260925C00100000", 1, "sell", "sell_to_close"),
            ExitLeg("SPY260925C00105000", 1, "buy", "buy_to_close"),
        ),
        "time_stop", "test",
    )


def priced_vertical_exit_plan() -> ExitPlan:
    plan = vertical_exit_plan()
    return price_exit_plan(
        plan,
        {
            plan.legs[0].symbol: exit_quote(
                plan.legs[0].symbol, bid=4.90, ask=5.00,
            ),
            plan.legs[1].symbol: exit_quote(
                plan.legs[1].symbol, bid=2.90, ask=3.00,
            ),
        },
        SETTINGS,
        as_of=AS_OF,
    )


class TestStableStrategyIdentity(unittest.TestCase):
    def test_given_same_strategy_and_session_then_client_id_is_repeatable(self):
        item = structure()
        session = date(2026, 9, 2)

        first = execute.strategy_client_order_id(item, session)
        second = execute.strategy_client_order_id(item, session)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 48)
        self.assertNotEqual(
            first, execute.strategy_client_order_id(structure(6), session)
        )
        self.assertNotEqual(
            first, execute.strategy_client_order_id(item, date(2026, 9, 3))
        )

    def test_given_dry_run_then_no_cli_is_required_and_stable_id_enters_payload(self):
        item = structure()
        expected = execute.strategy_client_order_id(item, date(2026, 9, 2))

        with patch("bookbound.execute.cli_available", return_value=False):
            result = execute.submit(
                item, dry_run=True, trading_date=date(2026, 9, 2),
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.client_order_id, expected)
        self.assertEqual(result.payload["client_order_id"], expected)

    def test_given_atomic_close_dry_run_then_no_cli_is_required(self):
        plan = priced_vertical_exit_plan()

        with patch("bookbound.execute.cli_available", return_value=False):
            result = execute.close_structure(plan, dry_run=True)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "DRY_RUN")
        self.assertEqual(result.payload["order_class"], "mleg")
        self.assertEqual(result.payload["type"], "limit")
        self.assertEqual(result.payload["limit_price"], "-1.90")

    def test_given_same_close_intent_and_session_then_client_id_is_repeatable(self):
        plan = priced_vertical_exit_plan()
        session = date(2026, 9, 2)

        first = execute.close_client_order_id(plan, session)
        second = execute.close_client_order_id(plan, session)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 48)
        self.assertNotEqual(
            first, execute.close_client_order_id(plan, date(2026, 9, 3)),
        )
        self.assertNotEqual(
            first, execute.close_client_order_id(plan, session, revision=1),
        )


class TestBoundedClosePricing(unittest.TestCase):
    def test_given_unpriced_structure_close_then_payload_fails_closed(self):
        with self.assertRaisesRegex(
            execute.ExecutionError, "verified quote-derived limit",
        ):
            execute.build_close_payload(
                vertical_exit_plan(), client_order_id="bb-test-close",
            )

    def test_given_synchronous_quotes_then_close_uses_executable_natural_price(self):
        plan = priced_vertical_exit_plan()

        payload = execute.build_close_payload(
            plan, client_order_id="bb-test-close",
        )

        # Sell the long at its bid (4.90), buy the short at its ask (3.00):
        # a 1.90 net credit, represented by Alpaca's negative MLEG convention.
        self.assertEqual(plan.limit_price, -1.90)
        self.assertEqual(payload["type"], "limit")
        self.assertEqual(payload["limit_price"], "-1.90")
        self.assertNotEqual(payload["type"], "market")

    def test_given_stale_leg_quote_then_no_reliable_limit_is_created(self):
        plan = vertical_exit_plan()
        stale = AS_OF - timedelta(minutes=10)
        quotes = {
            plan.legs[0].symbol: exit_quote(
                plan.legs[0].symbol, bid=4.90, ask=5.00, timestamp=stale,
            ),
            plan.legs[1].symbol: exit_quote(
                plan.legs[1].symbol, bid=2.90, ask=3.00,
            ),
        }

        with self.assertRaisesRegex(ValueError, "stale"):
            price_exit_plan(plan, quotes, SETTINGS, as_of=AS_OF)

    def test_given_missing_executable_side_size_then_limit_fails_closed(self):
        plan = vertical_exit_plan()
        quotes = {
            plan.legs[0].symbol: exit_quote(
                plan.legs[0].symbol, bid=4.90, ask=5.00, bid_size=0,
            ),
            plan.legs[1].symbol: exit_quote(
                plan.legs[1].symbol, bid=2.90, ask=3.00,
            ),
        }

        with self.assertRaisesRegex(ValueError, "executable bid size"):
            price_exit_plan(plan, quotes, SETTINGS, as_of=AS_OF)

    def test_given_naked_short_repair_then_buy_to_close_is_ask_bounded(self):
        plan = ExitPlan(
            "SPY", EXPIRY, "C",
            (ExitLeg(
                "SPY260925C00105000", 2, "buy", "buy_to_close",
            ),),
            "reconciliation_repair", "naked short",
        )
        priced = price_exit_plan(
            plan,
            {plan.legs[0].symbol: exit_quote(
                plan.legs[0].symbol, bid=2.90, ask=3.00,
            )},
            SETTINGS,
            as_of=AS_OF,
        )

        payload = execute.build_close_payload(
            priced, client_order_id="bb-test-naked",
        )

        self.assertNotIn("order_class", payload)
        self.assertEqual(payload["type"], "limit")
        self.assertEqual(payload["limit_price"], "3.00")


class TestStaleWorkingOrders(unittest.TestCase):
    def test_given_old_opening_order_then_it_is_selected_for_cancel_replace(self):
        now = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
        old = {
            "id": "old", "status": "new",
            "created_at": (now - timedelta(minutes=10)).isoformat(),
            "legs": [{"position_intent": "buy_to_open"}],
        }
        closing = {
            "id": "close", "status": "new",
            "created_at": (now - timedelta(minutes=10)).isoformat(),
            "legs": [{"position_intent": "sell_to_close"}],
        }
        recent = {
            "id": "recent", "status": "accepted",
            "created_at": (now - timedelta(seconds=20)).isoformat(),
            "legs": [{"position_intent": "buy_to_open"}],
        }

        stale = execute.stale_opening_orders(
            [recent, closing, old], as_of=now, max_age_seconds=300,
        )

        self.assertEqual([order["id"] for order in stale], ["old"])

    def test_given_stale_order_cancel_dry_run_then_broker_is_not_called(self):
        with patch("bookbound.execute.subprocess.run") as run:
            result = execute.cancel_order("order-123", dry_run=True)

        run.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.order_id, "order-123")
        self.assertEqual(result.status, "DRY_RUN")


class TestBrokerAcknowledgement(unittest.TestCase):
    def test_given_nonzero_cli_exit_then_empty_json_is_not_success(self):
        process = SimpleNamespace(returncode=1, stdout="{}", stderr="cli failed")
        with (
            patch("bookbound.execute.cli_available", return_value=True),
            patch("bookbound.execute.subprocess.run", return_value=process),
        ):
            result = execute.submit(structure(), dry_run=False, settings=SETTINGS)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "CLI_ERROR")

    def test_given_terminal_broker_status_then_order_is_not_success(self):
        process = SimpleNamespace(
            returncode=0,
            stdout='{"id":"order-1","status":"rejected"}',
            stderr="",
        )
        with (
            patch("bookbound.execute.cli_available", return_value=True),
            patch("bookbound.execute.subprocess.run", return_value=process),
        ):
            result = execute.submit(structure(), dry_run=False, settings=SETTINGS)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "REJECTED")

    def test_given_zero_exit_id_and_accepted_status_then_order_is_success(self):
        process = SimpleNamespace(
            returncode=0,
            stdout='{"id":"order-1","status":"accepted"}',
            stderr="",
        )
        with (
            patch("bookbound.execute.cli_available", return_value=True),
            patch("bookbound.execute.subprocess.run", return_value=process),
        ):
            result = execute.submit(structure(), dry_run=False, settings=SETTINGS)

        self.assertTrue(result.ok)
        self.assertEqual(result.order_id, "order-1")

    def test_given_live_submission_without_bound_settings_then_execution_refuses(self):
        with self.assertRaisesRegex(execute.ExecutionError, "bound Settings"):
            execute.submit(structure(), dry_run=False)

    def test_given_bound_settings_then_cli_receives_authoritative_credentials(self):
        process = SimpleNamespace(
            returncode=0,
            stdout='{"id":"order-1","status":"accepted"}',
            stderr="",
        )
        with (
            patch("bookbound.execute.cli_available", return_value=True),
            patch("bookbound.execute.subprocess.run", return_value=process) as run,
        ):
            execute.submit(structure(), dry_run=False, settings=SETTINGS)

        env = run.call_args.kwargs["env"]
        self.assertEqual(env["ALPACA_API_KEY"], SETTINGS.api_key)
        self.assertEqual(env["ALPACA_SECRET_KEY"], SETTINGS.secret_key)
        self.assertEqual(env["ALPACA_LIVE_TRADE"], "false")


if __name__ == "__main__":
    unittest.main(verbosity=2)
