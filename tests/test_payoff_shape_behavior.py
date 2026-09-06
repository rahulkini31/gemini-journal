"""BDD specifications for payoff geometry and factor-aware diversification."""
from __future__ import annotations

import dataclasses
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.market import Contract  # noqa: E402
from bookbound.structures import (  # noqa: E402
    Structure,
    build_vertical_credit_spreads,
    build_vertical_debit_spreads,
    execution_cost,
    quality_score,
    select_diversified_shortlist,
    submitted_execution_cost,
)


EXPIRY = date.today() + timedelta(days=30)
SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())


def contract(
    symbol: str,
    strike: float,
    delta: float,
    *,
    underlying: str = "SPY",
    expiry: date = EXPIRY,
    bid: float,
    ask: float,
) -> Contract:
    return Contract(
        symbol, underlying, expiry, "C", strike, bid, ask, delta,
        .01, -.10, .05, .20,
    )


def credit_candidate(
    label: str,
    *,
    width: float,
    credit: float,
    underlying: str = "SPY",
    expiry: date = EXPIRY,
    direction: str = "bear",
) -> Structure:
    short_strike = 100.0
    short = contract(
        f"{underlying}_{label}_SHORT", short_strike, .25,
        underlying=underlying, expiry=expiry, bid=3.00, ask=3.10,
    )
    long_ask = short.bid - credit
    long = contract(
        f"{underlying}_{label}_LONG", short_strike + width, .10,
        underlying=underlying, expiry=expiry,
        bid=long_ask - .10, ask=long_ask,
    )
    return Structure(
        name="bear_call_spread" if direction == "bear" else "bull_put_spread",
        underlying=underlying,
        expiry=expiry,
        qty=1,
        long=long,
        short=short,
        net_debit=-credit,
        max_loss=(width - credit) * 100,
        max_gain=credit * 100,
        spot=100,
        realised_vol=.18,
        forward=100,
    )


class TestHardPayoffShapeFloors(unittest.TestCase):
    def test_given_tiny_credit_for_width_then_builder_rejects_tail_heavy_spread(self):
        short = contract("SHORT", 100, .25, bid=1.00, ask=1.05)
        wing = contract("WING", 105, .10, bid=.45, ask=.50)

        built = build_vertical_credit_spreads(
            [short, wing], SETTINGS, kind="C", target_deltas=(.25,),
            anchor_delta_tolerance=.01, max_candidates=None,
            spot=100, realised_vols={"SPY": .18},
        )

        self.assertEqual(built, [])

    def test_given_debit_consumes_nearly_all_width_then_builder_rejects_it(self):
        long = contract("LONG", 100, .45, bid=4.45, ask=4.50)
        short = contract("SHORT", 105, .25, bid=.05, ask=.10)

        built = build_vertical_debit_spreads(
            [long, short], SETTINGS, kind="C", target_deltas=(.45,),
            anchor_delta_tolerance=.01, max_candidates=None,
            spot=100, realised_vols={"SPY": .18},
        )

        self.assertEqual(built, [])


class TestExecutionRankingDoesNotRewardTailSize(unittest.TestCase):
    def test_given_same_anchor_far_wing_then_larger_tail_cannot_improve_score(self):
        near = credit_candidate("NEAR", width=5, credit=1.00)
        far = credit_candidate("FAR", width=10, credit=1.20)
        # Under max-loss normalization the same $10 crossing cost made the
        # $880-risk wing look better than the $400-risk wing.
        self.assertGreater(far.max_loss, near.max_loss)
        self.assertGreater(quality_score(near), quality_score(far))

    def test_given_half_cross_limit_then_reported_execution_cost_matches_submission(self):
        item = credit_candidate("LIMIT", width=5, credit=1.00)
        mid = item.long.mid - item.short.mid
        expected = abs(item.limit_price() - mid) * 100 * item.qty

        self.assertAlmostEqual(submitted_execution_cost(item), expected)
        self.assertGreater(execution_cost(item), submitted_execution_cost(item))


class TestFactorAwareDiversification(unittest.TestCase):
    def test_given_index_etf_lookalikes_across_expiries_then_factor_cap_applies(self):
        candidates = [
            credit_candidate("S1", width=5, credit=1.00, underlying="SPY"),
            credit_candidate(
                "Q1", width=5, credit=1.00, underlying="QQQ",
                expiry=EXPIRY + timedelta(days=7),
            ),
            credit_candidate(
                "I1", width=5, credit=1.00, underlying="IWM",
                expiry=EXPIRY + timedelta(days=14),
            ),
            credit_candidate("G1", width=5, credit=1.00, underlying="GLD"),
        ]

        selected = select_diversified_shortlist(
            candidates,
            top_k=4,
            max_per_underlying_expiry_direction=None,
            max_per_anchor=None,
            apply_pareto=False,
            correlation_groups=(("SPY", "QQQ", "IWM"),),
            max_per_factor_direction=2,
            max_per_factor_direction_dte=1,
            dte_bucket_days=7,
        )

        index_names = {"SPY", "QQQ", "IWM"}
        self.assertLessEqual(
            sum(item.underlying in index_names for item in selected), 2,
        )
        self.assertIn("GLD", {item.underlying for item in selected})


class TestModelReceivesExecutableQuoteEvidence(unittest.TestCase):
    def test_given_candidate_then_summary_exposes_both_leg_quotes_and_greeks(self):
        item = credit_candidate("PROVENANCE", width=5, credit=1.00)
        observed = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
        item = dataclasses.replace(
            item,
            long=dataclasses.replace(
                item.long, quote_timestamp=observed,
                bid_size=12, ask_size=15, bid_exchange="U", ask_exchange="I",
                quote_condition=("R",),
            ),
            short=dataclasses.replace(
                item.short, quote_timestamp=observed + timedelta(seconds=1),
                bid_size=20, ask_size=25, bid_exchange="W", ask_exchange="P",
                quote_condition="R",
            ),
        )

        quote = item.summary()["quote_provenance"]

        self.assertEqual(quote["cross_leg_skew_seconds"], 1.0)
        self.assertEqual(quote["long"]["bid_size"], 12)
        self.assertEqual(quote["long"]["quote_timestamp"], observed.isoformat())
        self.assertEqual(quote["short"]["ask_exchange"], "P")
        for name in ("delta", "gamma", "theta", "vega", "iv"):
            self.assertIn(name, quote["long"]["greeks"])

    def test_given_missing_scenario_inputs_then_deterministic_breakeven_remains_visible(self):
        item = dataclasses.replace(
            credit_candidate("BREAKEVEN", width=5, credit=1.00),
            spot=0.0, realised_vol=0.0, forward=0.0,
        )

        summary = item.summary()

        self.assertFalse(summary["terminal_expiry_scenario"]["valid"])
        self.assertEqual(summary["expiration_breakeven"], 101.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
