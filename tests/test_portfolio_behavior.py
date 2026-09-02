"""Behavior scenarios for portfolio-level option risk.

Each scenario is intentionally phrased Given/When/Then.  These are regression
contracts for failures found in the deterministic-shortlist audit.
"""
from __future__ import annotations

import math
import dataclasses
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.book import (  # noqa: E402
    Book,
    Leg,
    dollar_delta_by_underlying,
    greek_stress_loss,
    option_portfolio_max_loss,
    option_portfolio_remaining_loss,
    pending_order_max_loss,
    missing_greeks,
)
from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.market import Contract  # noqa: E402
from bookbound.risk import evaluate, shrink_to_fit  # noqa: E402
from bookbound.structures import _price_debit_spread  # noqa: E402


EXPIRY = date.today() + timedelta(days=30)
SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())


def occ(strike: int, kind: str = "C", root: str = "SPY") -> str:
    return f"{root}{EXPIRY:%y%m%d}{kind}{strike * 1000:08d}"


def position(strike: int, *, qty: int, side: str, entry: float,
             kind: str = "C", root: str = "SPY") -> dict:
    return {
        "asset_class": "us_option",
        "symbol": occ(strike, kind, root),
        "qty": str(abs(qty)),
        "side": side,
        "avg_entry_price": str(entry),
    }


def contract(symbol: str, strike: float, delta: float) -> Contract:
    return Contract(
        symbol=symbol, underlying="SPY", expiry=EXPIRY, kind="C",
        strike=strike, bid=4.95, ask=5.05, delta=delta,
        gamma=0.01, theta=-0.20, vega=0.10, iv=0.20,
    )


def candidate():
    built = _price_debit_spread(
        contract(occ(760), 760, 0.55),
        Contract(
            symbol=occ(765), underlying="SPY", expiry=EXPIRY, kind="C",
            strike=765, bid=3.00, ask=3.10, delta=0.35,
            gamma=0.01, theta=-0.15, vega=0.08, iv=0.20,
        ),
        "C", EXPIRY, spot=762.0, realised_vol=0.18, forward=763.0,
    )
    assert built is not None
    return built


class TestMaximumLossReconstruction(unittest.TestCase):
    def test_given_debit_vertical_when_reconstructed_then_risk_is_net_debit(self):
        # Given a 760/765 call spread bought for 6 and sold for 3
        positions = [
            position(760, qty=1, side="long", entry=6.0),
            position(765, qty=1, side="short", entry=3.0),
        ]

        # When the complete payoff is evaluated, Then max loss is $300.
        self.assertAlmostEqual(option_portfolio_max_loss(positions), 300.0)

    def test_given_credit_vertical_when_reconstructed_then_risk_is_width_less_credit(self):
        # Given a 760/765 call spread sold for a net $2 credit
        positions = [
            position(760, qty=1, side="short", entry=5.0),
            position(765, qty=1, side="long", entry=3.0),
        ]

        # Then the remaining expiry loss is (5 - 2) * 100.
        self.assertAlmostEqual(option_portfolio_max_loss(positions), 300.0)

    def test_given_naked_short_call_then_risk_is_unbounded(self):
        positions = [position(760, qty=1, side="short", entry=5.0)]
        self.assertTrue(math.isinf(option_portfolio_max_loss(positions)))

    def test_given_profitable_spread_then_remaining_loss_uses_current_mark(self):
        # Bought for $2 net, but it is now worth $8 and current equity includes it.
        positions = [
            {
                **position(760, qty=1, side="long", entry=10.0),
                "market_value": "1000",
            },
            {
                **position(770, qty=1, side="short", entry=8.0),
                "market_value": "-200",
            },
        ]

        self.assertAlmostEqual(option_portfolio_max_loss(positions), 200.0)
        self.assertAlmostEqual(option_portfolio_remaining_loss(positions), 800.0)

    def test_given_missing_current_mark_then_remaining_loss_fails_closed(self):
        self.assertTrue(math.isinf(option_portfolio_remaining_loss([
            position(760, qty=1, side="long", entry=2.0),
        ])))


class TestWorkingOrderReservations(unittest.TestCase):
    @staticmethod
    def credit_order(width: int = 5, credit: float = 1.5) -> dict:
        return {
            "status": "new",
            "qty": "1",
            "limit_price": str(-credit),
            "legs": [
                {"symbol": occ(760), "side": "sell", "ratio_qty": "1",
                 "position_intent": "sell_to_open"},
                {"symbol": occ(760 + width), "side": "buy", "ratio_qty": "1",
                 "position_intent": "buy_to_open"},
            ],
        }

    def test_given_working_credit_spread_then_width_less_credit_is_reserved(self):
        self.assertAlmostEqual(pending_order_max_loss(self.credit_order()), 350.0)

    def test_given_single_leg_opening_order_then_its_risk_is_reserved(self):
        long_order = {
            "status": "new", "qty": "2", "limit_price": "1.25",
            "symbol": occ(760), "side": "buy",
            "position_intent": "buy_to_open",
        }
        short_order = {
            "status": "new", "qty": "1", "limit_price": "1.25",
            "symbol": occ(760), "side": "sell",
            "position_intent": "sell_to_open",
        }

        self.assertAlmostEqual(pending_order_max_loss(long_order), 250.0)
        self.assertTrue(math.isinf(pending_order_max_loss(short_order)))

    def test_given_cross_series_order_then_defined_risk_is_not_invented(self):
        order = {
            "status": "new", "qty": "1", "limit_price": "1.00",
            "legs": [
                {"symbol": occ(760), "side": "buy", "ratio_qty": "1",
                 "position_intent": "buy_to_open"},
                {"symbol": occ(765, root="IWM"), "side": "sell",
                 "ratio_qty": "1", "position_intent": "sell_to_open"},
            ],
        }

        self.assertTrue(math.isinf(pending_order_max_loss(order)))

    def test_given_partial_parent_fill_then_only_remaining_quantity_is_reserved(self):
        order = self.credit_order(width=5, credit=1.0)
        order.update({"qty": "5", "filled_qty": "2"})

        # Three spreads remain, each with (5 - 1) * 100 maximum loss.
        self.assertAlmostEqual(pending_order_max_loss(order), 1200.0)

    def test_given_unbounded_market_entry_then_pending_risk_is_unknown(self):
        order = {
            "status": "new", "qty": "1", "type": "market",
            "symbol": occ(760), "side": "buy",
            "position_intent": "buy_to_open",
        }

        self.assertTrue(math.isinf(pending_order_max_loss(order)))

    def test_given_working_order_when_candidate_is_gated_then_reserved_risk_is_automatic(self):
        # Given a working spread that consumes essentially the whole 5% book cap
        working = self.credit_order(width=50, credit=0.01)
        book = Book(
            equity=100_000, last_equity=100_000, cash=100_000,
            buying_power=400_000, options_buying_power=100_000,
            legs=[], raw_positions=[], raw_orders=[working], spots={"SPY": 762.0},
        )

        # When another candidate is evaluated, Then the unseen DAY order blocks it.
        verdict = evaluate(candidate(), book, SETTINGS)
        checks = {item["check"]: item for item in verdict.checks}
        self.assertFalse(checks["total_open_risk"]["passed"])
        self.assertGreater(checks["total_open_risk"]["pending"], 4_900)

    def test_given_same_legs_already_working_then_duplicate_candidate_is_refused(self):
        item = candidate()
        working = {
            "status": "new", "qty": "1", "limit_price": str(item.limit_price()),
            "legs": [
                {"symbol": item.long.symbol, "side": "buy", "ratio_qty": "1",
                 "position_intent": "buy_to_open"},
                {"symbol": item.short.symbol, "side": "sell", "ratio_qty": "1",
                 "position_intent": "sell_to_open"},
            ],
        }
        book = Book(
            equity=100_000, last_equity=100_000, cash=100_000,
            buying_power=400_000, options_buying_power=100_000,
            legs=[], raw_positions=[], raw_orders=[working], spots={"SPY": 762.0},
        )

        verdict = evaluate(item, book, SETTINGS)
        checks = {entry["check"]: entry for entry in verdict.checks}
        self.assertFalse(checks["duplicate_working_order"]["passed"])

    def test_given_pending_leg_without_greeks_then_book_is_unknown(self):
        working = self.credit_order()
        book = Book(
            equity=100_000, last_equity=100_000, cash=100_000,
            buying_power=400_000, options_buying_power=100_000,
            legs=[], raw_positions=[], raw_orders=[working],
        )

        unknown = missing_greeks(book, {})

        self.assertEqual(set(unknown), {occ(760), occ(765)})

    def test_given_unknown_book_then_sizing_cannot_bypass_fail_closed_gate(self):
        result = shrink_to_fit(
            candidate().at_qty(3),
            Book(100_000, 100_000, 100_000, 400_000, 100_000, [], []),
            SETTINGS,
            unpriced_positions=["UNKNOWN"],
        )

        self.assertIsNone(result)


class TestModelScenarioIsNotAnUncalibratedHardGate(unittest.TestCase):
    def test_given_default_budget_then_ev_is_reported_but_not_required(self):
        item = dataclasses.replace(candidate(), realised_vol=0.01, forward=700.0)
        verdict = evaluate(
            item,
            Book(100_000, 100_000, 100_000, 400_000, 100_000, [], []),
            SETTINGS,
        )
        check = next(c for c in verdict.checks if c["check"] == "positive_expectancy")
        self.assertTrue(check["passed"])
        self.assertFalse(check["required"])
        self.assertNotIn("p=", check["detail"])
        self.assertIn("terminal-expiry scenario", check["detail"])

    def test_given_explicit_calibrated_model_policy_then_negative_ev_is_refused(self):
        item = dataclasses.replace(
            candidate(), realised_vol=0.01, forward=700.0,
            scenario_calibrated=True, scenario_policy_aware=True,
        )
        strict = Settings(
            api_key="k", secret_key="s",
            risk=dataclasses.replace(RiskBudget(), require_positive_expectancy=True),
        )
        verdict = evaluate(
            item,
            Book(100_000, 100_000, 100_000, 400_000, 100_000, [], []),
            strict,
        )
        check = next(c for c in verdict.checks if c["check"] == "positive_expectancy")
        self.assertFalse(check["passed"])
        self.assertTrue(check["required"])

    def test_given_calibrated_policy_aware_positive_scenario_then_strict_gate_passes(self):
        item = dataclasses.replace(
            candidate(), scenario_calibrated=True, scenario_policy_aware=True,
        )
        strict = Settings(
            api_key="k", secret_key="s",
            risk=dataclasses.replace(RiskBudget(), require_positive_expectancy=True),
        )

        verdict = evaluate(
            item,
            Book(100_000, 100_000, 100_000, 400_000, 100_000, [], []),
            strict,
        )

        check = next(c for c in verdict.checks if c["check"] == "positive_expectancy")
        self.assertTrue(check["passed"])
        self.assertTrue(check["calibrated"])
        self.assertTrue(check["policy_aware"])

    def test_given_uncalibrated_positive_proxy_then_strict_gate_fails_closed(self):
        strict = Settings(
            api_key="k", secret_key="s",
            risk=dataclasses.replace(RiskBudget(), require_positive_expectancy=True),
        )

        verdict = evaluate(
            candidate(),
            Book(100_000, 100_000, 100_000, 400_000, 100_000, [], []),
            strict,
        )

        check = next(c for c in verdict.checks if c["check"] == "positive_expectancy")
        self.assertFalse(check["passed"])
        self.assertGreater(check["terminal_scenario_expected_pnl"], 0)
        self.assertFalse(check["calibrated"])


class TestEconomicExposure(unittest.TestCase):
    def test_given_equal_raw_deltas_on_different_etfs_then_dollar_risk_does_not_net(self):
        # Given +100 SPY delta and -100 IWM delta
        legs = [
            Leg(occ(760, root="SPY"), 1, 1.0, 0.0, 0.0, 0.0),
            Leg(occ(290, root="IWM"), -1, 1.0, 0.0, 0.0, 0.0),
        ]

        # Then a 1% move is measured in dollars per underlying, not raw shares.
        exposure = dollar_delta_by_underlying(
            legs, {"SPY": 762.0, "IWM": 291.0}, move_pct=0.01,
        )
        self.assertAlmostEqual(exposure["SPY"], 762.0)
        self.assertAlmostEqual(exposure["IWM"], -291.0)
        self.assertNotEqual(sum(exposure.values()), 0.0)

    def test_given_cross_underlying_offset_then_correlated_stress_retains_basis_risk(self):
        legs = [
            Leg(occ(760, root="SPY"), 1, 1.0, 0.0, 0.0, 0.0),
            Leg(occ(290, root="IWM"), -1, 1.0, 0.0, 0.0, 0.0),
        ]
        loss = greek_stress_loss(
            legs, {"SPY": 762.0, "IWM": 291.0},
            spot_shocks=(-0.03, 0.03), vol_shocks_points=(0.0,), days=0,
        )
        self.assertAlmostEqual(loss, 3 * (762.0 + 291.0), places=2)

    def test_given_exact_dollar_delta_offset_then_basis_stress_remains_nonzero(self):
        legs = [
            Leg(occ(760, root="SPY"), 1, 291 / 762, 0, 0, 0),
            Leg(occ(290, root="IWM"), -1, 1.0, 0, 0, 0),
        ]

        loss = greek_stress_loss(
            legs, {"SPY": 762.0, "IWM": 291.0},
            spot_shocks=(-.03, .03), vol_shocks_points=(0.0,), days=0,
        )

        self.assertAlmostEqual(loss, 2 * 291 * 3, places=2)

    def test_given_partial_spot_map_then_dollar_gate_fails_closed(self):
        existing = Leg(occ(290, root="IWM"), 1, .5, .01, -.1, .1)
        book = Book(
            100_000, 100_000, 100_000, 400_000, 100_000,
            [existing], [], spots={"SPY": 762.0},
        )

        verdict = evaluate(candidate(), book, SETTINGS)
        check = next(
            item for item in verdict.checks
            if item["check"] == "dollar_delta_by_underlying"
        )

        self.assertFalse(check["passed"])
        self.assertIn("missing", check["detail"].lower())

    def test_given_invalid_ev_model_then_observed_friction_still_gates(self):
        item = dataclasses.replace(
            candidate(),
            long=dataclasses.replace(candidate().long, bid=4.0, ask=5.0),
            short=dataclasses.replace(candidate().short, bid=2.0, ask=3.0),
            net_debit=3.0, max_loss=300.0,
            spot=0.0, realised_vol=0.0, forward=0.0,
        )

        strict = dataclasses.replace(
            SETTINGS,
            risk=dataclasses.replace(
                SETTINGS.risk, max_edge_erosion_pct=.10,
            ),
        )
        verdict = evaluate(item, Book(
            100_000, 100_000, 100_000, 400_000, 100_000, [], [],
        ), strict)
        check = next(c for c in verdict.checks if c["check"] == "edge_erosion")

        self.assertFalse(check["passed"])
        self.assertGreater(check["drag"], 0)

    def test_given_three_existing_contract_units_then_two_more_breach_four_unit_cap(self):
        # Given three existing option contract units on SPY and a two-leg candidate
        existing = [
            Leg(occ(750 + i), 1, 0.0, 0.0, 0.0, 0.0)
            for i in range(3)
        ]
        book = Book(
            equity=100_000, last_equity=100_000, cash=100_000,
            buying_power=400_000, options_buying_power=100_000,
            legs=existing, raw_positions=[], spots={"SPY": 762.0},
        )

        # When admitted quantity is considered, Then 3 + 2 cannot pass a cap of 4.
        verdict = evaluate(candidate(), book, SETTINGS)
        checks = {item["check"]: item for item in verdict.checks}
        self.assertFalse(checks["concentration"]["passed"])
        self.assertEqual(checks["concentration"]["after_contract_units"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
