"""Behaviour specifications for deterministic shortlist construction.

Each test uses Given/When/Then language deliberately: this is the executable
contract for the shortlist redesign, not a characterisation of the old bugs.
"""
from __future__ import annotations

import dataclasses
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.market import Contract  # noqa: E402
from bookbound import structures  # noqa: E402


SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())
EXPIRY = date.today() + timedelta(days=30)


def contract(
    symbol: str,
    strike: float,
    delta: float,
    *,
    underlying: str = "SPY",
    expiry: date = EXPIRY,
    kind: str = "C",
    bid: float = 3.00,
    ask: float = 3.10,
    iv: float = 0.20,
) -> Contract:
    return Contract(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        kind=kind,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        gamma=0.02,
        theta=-0.40,
        vega=0.10,
        iv=iv,
    )


def call_grid(count: int = 7) -> list[Contract]:
    """A liquid descending-delta call series with viable debit/credit pairs."""
    return [
        contract(
            f"SPY_C{i}",
            100.0 + i,
            0.55 - i * 0.05,
            bid=6.00 - i * 0.55,
            ask=6.08 - i * 0.55,
        )
        for i in range(count)
    ]


def candidate(
    label: str,
    *,
    underlying: str = "SPY",
    expiry: date = EXPIRY,
    name: str = "bull_call_spread",
    anchor: str | None = None,
    execution_gap: float = 0.02,
    max_loss: float = 200.0,
    max_gain: float = 300.0,
) -> structures.Structure:
    """Build a controllable candidate without invoking the spread pricer."""
    anchor = anchor or f"{label}_ANCHOR"
    long = contract(
        anchor,
        100.0,
        0.45,
        underlying=underlying,
        expiry=expiry,
        bid=4.95,
        ask=5.05,
    )
    short = contract(
        f"{label}_WING",
        105.0,
        0.25,
        underlying=underlying,
        expiry=expiry,
        bid=2.95 - execution_gap,
        ask=3.05 - execution_gap,
    )
    return structures.Structure(
        name=name,
        underlying=underlying,
        expiry=expiry,
        qty=1,
        long=long,
        short=short,
        net_debit=2.0,
        max_loss=max_loss,
        max_gain=max_gain,
        spot=102.0,
        realised_vol=0.18,
        forward=102.2,
    )


class TestDeltaGridAnchors(unittest.TestCase):
    def test_debit_builder_uses_multiple_unique_anchors(self):
        """Given a two-point delta grid, when building, then both anchors appear."""
        # Given
        pool = call_grid()

        # When
        built = structures.build_vertical_debit_spreads(
            pool,
            SETTINGS,
            kind="C",
            target_deltas=(0.55, 0.45),
            anchor_delta_tolerance=0.01,
            max_candidates=None,
            spot=102.0,
            realised_vols={"SPY": 0.18},
        )

        # Then
        self.assertEqual({item.long.symbol for item in built}, {"SPY_C0", "SPY_C2"})

    def test_credit_builder_uses_explicit_grid_and_rejects_distant_anchor(self):
        """Given targets and tolerance, only a contract close enough can anchor."""
        # Given
        pool = call_grid()

        # When
        built = structures.build_vertical_credit_spreads(
            pool,
            SETTINGS,
            kind="C",
            target_deltas=(0.45, 0.80),
            anchor_delta_tolerance=0.01,
            max_candidates=None,
            spot=102.0,
            realised_vols={"SPY": 0.18},
        )

        # Then
        self.assertTrue(built)
        self.assertEqual({item.short.symbol for item in built}, {"SPY_C2"})

    def test_anchor_choice_is_unique_and_independent_of_chain_order(self):
        """Given overlapping targets, selection is stable and never reuses a leg."""
        # Given
        pool = call_grid(4)

        # When
        forward = structures.select_delta_anchors(pool, (0.50, 0.51), tolerance=0.06)
        reverse = structures.select_delta_anchors(
            list(reversed(pool)), (0.50, 0.51), tolerance=0.06
        )

        # Then
        self.assertEqual([item.symbol for item in forward], [item.symbol for item in reverse])
        self.assertEqual(len({item.symbol for item in forward}), 2)


class TestGenerationIsNotPrematurelyTruncated(unittest.TestCase):
    def test_none_returns_the_complete_candidate_set(self):
        """Given many viable pairs, None means no local pre-gate truncation."""
        # Given
        pool = call_grid(7)

        # When
        complete = structures.build_vertical_debit_spreads(
            pool,
            SETTINGS,
            kind="C",
            target_deltas=(0.55, 0.50, 0.45),
            anchor_delta_tolerance=0.01,
            max_candidates=None,
            spot=102.0,
            realised_vols={"SPY": 0.18},
        )
        capped = structures.build_vertical_debit_spreads(
            pool,
            SETTINGS,
            kind="C",
            target_deltas=(0.55, 0.50, 0.45),
            anchor_delta_tolerance=0.01,
            max_candidates=3,
            spot=102.0,
            realised_vols={"SPY": 0.18},
        )

        # Then
        self.assertGreater(len(complete), 3)
        self.assertEqual(len(capped), 3)


class TestFreshPairConstruction(unittest.TestCase):
    def test_given_explicit_as_of_then_stale_quotes_never_form_a_candidate(self):
        """Given old quotes, strict construction must fail closed before pairing."""
        now = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
        expiry = date(2026, 9, 25)
        pool = [
            dataclasses.replace(
                item,
                expiry=expiry,
                quote_timestamp=now - timedelta(minutes=5),
                bid_size=10,
                ask_size=10,
            )
            for item in call_grid(4)
        ]

        built = structures.build_vertical_debit_spreads(
            pool, SETTINGS, kind="C", target_deltas=(0.55,),
            anchor_delta_tolerance=0.01, max_candidates=None, spot=102.0,
            realised_vols={"SPY": 0.18}, as_of=now,
        )

        self.assertEqual(built, [])

    def test_given_individually_fresh_but_async_legs_then_pair_is_rejected(self):
        """Given page-skewed quotes, no synthetic two-time market may be priced."""
        now = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
        expiry = date(2026, 9, 25)
        base = call_grid(2)
        pool = [
            dataclasses.replace(
                base[0], expiry=expiry, quote_timestamp=now,
                bid_size=10, ask_size=10,
            ),
            dataclasses.replace(
                base[1], expiry=expiry,
                quote_timestamp=now - timedelta(seconds=45),
                bid_size=10, ask_size=10,
            ),
        ]

        built = structures.build_vertical_debit_spreads(
            pool, SETTINGS, kind="C", target_deltas=(0.55,),
            anchor_delta_tolerance=0.01, max_candidates=None, spot=102.0,
            realised_vols={"SPY": 0.18}, as_of=now,
        )

        self.assertEqual(built, [])

    def test_given_frozen_exchange_date_then_structure_dte_ignores_wall_clock(self):
        valuation_date = date(2026, 9, 2)
        expiry = date(2026, 9, 25)
        long = contract("LONG", 100, .55, expiry=expiry, bid=5, ask=5.1)
        short = contract("SHORT", 105, .30, expiry=expiry, bid=3, ask=3.1)

        built = structures._price_debit_spread(
            long, short, "C", expiry, spot=102, realised_vol=.18,
            forward=102.2, valuation_date=valuation_date,
        )

        self.assertIsNotNone(built)
        self.assertEqual(built.dte, 23)
        self.assertEqual(built.summary()["dte"], 23)

    def test_given_new_selected_leg_quotes_then_complete_structure_is_repriced(self):
        original = candidate("REPRICE")
        refreshed_long = dataclasses.replace(
            original.long, ask=original.long.ask + .20, delta=.50,
        )
        refreshed_short = dataclasses.replace(original.short, delta=.20)

        repriced = structures.reprice_structure(
            original,
            {refreshed_long.symbol: refreshed_long,
             refreshed_short.symbol: refreshed_short},
            spot=103.0,
            valuation_date=date(2026, 9, 2),
        )

        self.assertIsNotNone(repriced)
        assert repriced is not None
        self.assertAlmostEqual(
            repriced.net_debit, refreshed_long.ask - refreshed_short.bid,
        )
        self.assertAlmostEqual(repriced.max_loss, repriced.net_debit * 100)
        self.assertEqual(repriced.long.delta, .50)
        self.assertEqual(repriced.short.delta, .20)
        self.assertEqual(repriced.spot, 103.0)
        self.assertEqual(repriced.valuation_date, date(2026, 9, 2))


class TestCreditWingBounds(unittest.TestCase):
    def test_given_near_zero_delta_hedge_then_credit_builder_rejects_it(self):
        """A token penny wing must not make an effectively naked short eligible."""
        short = contract("SHORT", 100, .25, bid=3.0, ask=3.1)
        token_wing = contract("TOKEN", 105, .01, bid=.09, ask=.10)

        built = structures.build_vertical_credit_spreads(
            [short, token_wing], SETTINGS, kind="C", target_deltas=(.25,),
            anchor_delta_tolerance=.01, max_candidates=None, spot=102,
            realised_vols={"SPY": .18},
        )

        self.assertEqual(built, [])


class TestModelInputProvenance(unittest.TestCase):
    def test_pricer_does_not_substitute_strike_or_implied_vol(self):
        """Given missing model inputs, construction succeeds but EV fails closed."""
        # Given
        long = contract("LONG", 100.0, 0.45, bid=5.0, ask=5.1)
        short = contract("SHORT", 105.0, 0.25, bid=3.0, ask=3.1)

        # When
        built = structures._price_debit_spread(long, short, "C", EXPIRY)

        # Then
        self.assertIsNotNone(built)
        assert built is not None
        self.assertEqual(built.spot, 0.0)
        self.assertEqual(built.realised_vol, 0.0)
        self.assertFalse(built.ev().valid)
        model = built.summary()["ev_model"]
        self.assertFalse(model["valid"])
        self.assertEqual(
            model["missing_inputs"], ["spot", "realised_vol", "forward"]
        )
        self.assertEqual(model["inputs"]["realised_vol"]["source"], "missing")

    def test_builder_labels_observed_input_sources(self):
        """Given observed inputs, the LLM summary identifies their provenance."""
        # Given
        calls = call_grid(4)
        puts = [
            contract(
                f"SPY_P{i}",
                call.strike,
                -0.45,
                kind="P",
                bid=call.bid - 0.2,
                ask=call.ask - 0.2,
            )
            for i, call in enumerate(calls)
        ]

        # When
        built = structures.build_vertical_debit_spreads(
            calls + puts,
            SETTINGS,
            kind="C",
            target_deltas=(0.55,),
            anchor_delta_tolerance=0.01,
            max_candidates=None,
            spot=102.0,
            realised_vols={"SPY": 0.18},
        )

        # Then
        self.assertTrue(built)
        model = built[0].summary()["ev_model"]
        self.assertTrue(model["valid"])
        self.assertEqual(model["inputs"]["spot"]["source"], "underlying_spot")
        self.assertEqual(
            model["inputs"]["realised_vol"]["source"], "historical_realised_vol"
        )
        self.assertEqual(
            model["inputs"]["forward"]["source"], "put_call_parity"
        )


class TestExecutionOnlyRanking(unittest.TestCase):
    def test_quality_score_never_consults_probability_or_ev(self):
        """Given an EV model failure, execution ranking remains deterministic."""
        # Given
        item = candidate("A")

        # When / Then
        with patch.object(
            structures,
            "probability_of_profit",
            side_effect=AssertionError("PoP must not enter execution ranking"),
        ), patch.object(
            structures,
            "expected_value",
            side_effect=AssertionError("EV must not enter execution ranking"),
        ):
            self.assertIsInstance(structures.quality_score(item), float)

    def test_lower_normalised_execution_cost_ranks_first(self):
        """Given otherwise equal trades, lower observable friction wins."""
        # Given
        cheap = candidate("CHEAP", execution_gap=0.01)
        expensive = candidate("EXPENSIVE", execution_gap=0.20)

        # When / Then
        self.assertGreater(
            structures.quality_score(cheap), structures.quality_score(expensive)
        )


class TestParetoPruning(unittest.TestCase):
    def test_dominance_is_applied_only_inside_a_comparable_cluster(self):
        """Given one dominated wing, it is pruned without crossing exposures."""
        # Given
        best = candidate(
            "BEST", anchor="SHARED", execution_gap=0.01, max_loss=150, max_gain=350
        )
        dominated = candidate(
            "DOMINATED",
            anchor="SHARED",
            execution_gap=0.20,
            max_loss=200,
            max_gain=300,
        )
        other_expiry = candidate(
            "OTHER",
            expiry=EXPIRY + timedelta(days=7),
            execution_gap=0.30,
            max_loss=250,
            max_gain=250,
        )

        # When
        kept = structures.pareto_prune([dominated, other_expiry, best])

        # Then
        self.assertEqual({item.key for item in kept}, {best.key, other_expiry.key})


class TestDiversifiedTopK(unittest.TestCase):
    def test_caps_cluster_and_anchor_with_stable_tie_breaks(self):
        """Given concentrated leaders, final top-K diversifies deterministically."""
        # Given: three SPY bullish leaders, including two variants of one anchor.
        candidates = [
            candidate("A1", anchor="ANCHOR_A", execution_gap=0.01),
            candidate("A2", anchor="ANCHOR_A", execution_gap=0.02),
            candidate("B", anchor="ANCHOR_B", execution_gap=0.03),
            candidate("C", anchor="ANCHOR_C", execution_gap=0.04),
            candidate(
                "Q",
                underlying="QQQ",
                anchor="ANCHOR_Q",
                execution_gap=0.05,
            ),
        ]

        # When
        selected = structures.select_diversified_shortlist(
            candidates,
            3,
            max_per_underlying_expiry_direction=2,
            max_per_anchor=1,
            apply_pareto=False,
        )
        reversed_selected = structures.select_diversified_shortlist(
            list(reversed(candidates)),
            3,
            max_per_underlying_expiry_direction=2,
            max_per_anchor=1,
            apply_pareto=False,
        )

        # Then
        self.assertEqual(
            [item.key for item in selected], [item.key for item in reversed_selected]
        )
        self.assertEqual([item.long.symbol for item in selected].count("ANCHOR_A"), 1)
        spy = [item for item in selected if item.underlying == "SPY"]
        self.assertEqual(len(spy), 2)
        self.assertEqual(len(selected), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
