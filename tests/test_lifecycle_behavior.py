"""Behaviour specifications for safe option-position lifecycle management.

These scenarios deliberately exercise public, side-effect-free planning APIs.
They do not submit orders or call Alpaca.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.config import RiskBudget, Settings  # noqa: E402


SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())
TODAY = date(2026, 9, 2)
FAR_EXPIRY = date(2026, 9, 30)


def option_position(
    symbol: str,
    qty: int = 1,
    *,
    side: str = "long",
    cost_basis: float = 0.0,
    unrealized_pl: float = 0.0,
) -> dict:
    return {
        "asset_class": "us_option",
        "symbol": symbol,
        "qty": str(abs(qty)),
        "side": side,
        "cost_basis": str(cost_basis),
        "unrealized_pl": str(unrealized_pl),
    }



def _priced(plan):
    """Attach a verified quote-derived limit.

    build_close_payload refuses any plan without one - an exit must never be
    submitted unbounded. These scenarios are about payload SHAPE, so they take
    the limit as given rather than re-testing pricing.
    """
    from dataclasses import replace as _replace

    from bookbound.exits import QUOTE_NATURAL_LIMIT_SOURCE
    return _replace(
        plan,
        limit_price=-1.25,
        limit_source=QUOTE_NATURAL_LIMIT_SOURCE,
        limit_as_of=datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc),
    )


class DescribeSpreadAtomicExitEvaluation(unittest.TestCase):
    def far_vertical(self, *, long_pl: float, short_pl: float) -> list[dict]:
        return [
            option_position(
                "SPY260930C00600000", cost_basis=300.0,
                unrealized_pl=long_pl,
            ),
            option_position(
                "SPY260930C00605000", side="short", cost_basis=100.0,
                unrealized_pl=short_pl,
            ),
        ]

    def test_given_one_winning_leg_but_flat_spread_when_evaluated_then_nothing_closes(self):
        # Given a long leg above its individual +50% target, offset by its hedge.
        positions = self.far_vertical(long_pl=170.0, short_pl=-160.0)

        # When the lifecycle evaluates the position as a spread.
        from bookbound.exits import evaluate_structure_exits
        plans = evaluate_structure_exits(positions, SETTINGS, today=TODAY)

        # Then it must not strand the short leg by selling only the long leg.
        self.assertEqual(plans, [])

    def test_given_profitable_vertical_when_evaluated_then_both_legs_close_together(self):
        # Given aggregate P&L +130 on a net cost basis of 200 (+65%).
        positions = self.far_vertical(long_pl=170.0, short_pl=-40.0)

        # When the group-level profit ladder is evaluated.
        from bookbound.exits import evaluate_structure_exits
        plans = evaluate_structure_exits(positions, SETTINGS, today=TODAY)

        # Then one atomic plan closes the complete vertical with close intents.
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.reason, "profit_target")
        self.assertAlmostEqual(plan.unrealized_pl, 130.0)
        self.assertAlmostEqual(plan.cost_basis, 200.0)
        self.assertEqual(len(plan.legs), 2)
        by_symbol = {leg.symbol: leg for leg in plan.legs}
        self.assertEqual(by_symbol["SPY260930C00600000"].side, "sell")
        self.assertEqual(
            by_symbol["SPY260930C00600000"].position_intent,
            "sell_to_close",
        )
        self.assertEqual(by_symbol["SPY260930C00605000"].side, "buy")
        self.assertEqual(
            by_symbol["SPY260930C00605000"].position_intent,
            "buy_to_close",
        )

    def test_given_losing_vertical_when_evaluated_then_stop_closes_complete_spread(self):
        # Given aggregate P&L -140 on a net cost basis of 200 (-70%).
        positions = self.far_vertical(long_pl=-170.0, short_pl=30.0)

        # When the group-level stop is evaluated.
        from bookbound.exits import evaluate_structure_exits
        plans = evaluate_structure_exits(positions, SETTINGS, today=TODAY)

        # Then the stop is a single plan containing both legs.
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].reason, "stop_loss")
        self.assertEqual(len(plans[0].legs), 2)

    def test_given_vertical_inside_time_floor_then_calendar_exit_closes_both_legs(self):
        # Given a balanced vertical at two DTE with no usable cost basis.
        positions = [
            option_position("SPY260904P00590000"),
            option_position("SPY260904P00595000", side="short"),
        ]

        # When the absolute time stop is evaluated.
        from bookbound.exits import evaluate_structure_exits
        plans = evaluate_structure_exits(positions, SETTINGS, today=TODAY)

        # Then time wins over P&L availability and the whole spread closes.
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].reason, "time_stop")
        self.assertEqual(len(plans[0].legs), 2)

    def test_given_mixed_positions_when_grouped_then_series_never_cross(self):
        # Given positions sharing dates but not all underlying and option kind.
        positions = [
            option_position("SPY260930C00600000"),
            option_position("SPY260930C00605000", side="short"),
            option_position("SPY260930P00590000"),
            option_position("QQQ260930C00500000"),
        ]

        # When deterministic position grouping runs.
        from bookbound.exits import group_option_positions
        groups = group_option_positions(positions)

        # Then each (underlying, expiry, kind) is a separate lifecycle unit.
        self.assertEqual(
            {(g.underlying, g.expiry, g.kind) for g in groups},
            {
                ("SPY", FAR_EXPIRY, "C"),
                ("SPY", FAR_EXPIRY, "P"),
                ("QQQ", FAR_EXPIRY, "C"),
            },
        )

    def test_given_unbalanced_group_when_exit_trigger_hits_then_reconciliation_owns_it(self):
        # Given a broken 2:1 fill whose apparent P&L crosses the target.
        positions = [
            option_position(
                "SPY260930C00600000", qty=1, cost_basis=300,
                unrealized_pl=500,
            ),
            option_position(
                "SPY260930C00605000", qty=2, side="short", cost_basis=200,
                unrealized_pl=-50,
            ),
        ]

        # When normal exit management runs.
        from bookbound.exits import evaluate_structure_exits
        plans = evaluate_structure_exits(positions, SETTINGS, today=TODAY)

        # Then it fails closed instead of guessing which leg is the spread.
        self.assertEqual(plans, [])


class DescribeAtomicClosePayload(unittest.TestCase):
    def test_given_three_balanced_spreads_when_built_then_one_mleg_closes_all(self):
        # Given a structure-level close plan for three identical verticals.
        from bookbound.exits import ExitLeg, ExitPlan
        plan = ExitPlan(
            underlying="SPY",
            expiry=FAR_EXPIRY,
            kind="C",
            legs=(
                ExitLeg("SPY260930C00600000", 3, "sell", "sell_to_close"),
                ExitLeg("SPY260930C00605000", 3, "buy", "buy_to_close"),
            ),
            reason="profit_target",
            detail="spread target reached",
        )

        # When the broker payload is built without submitting it.
        from bookbound.execute import build_close_payload
        payload = build_close_payload(_priced(plan), client_order_id="bb-test-close")

        # Then a single atomic MLEG carries base quantity three and 1:1 ratios.
        self.assertEqual(payload["order_class"], "mleg")
        self.assertEqual(payload["qty"], "3")
        self.assertEqual(payload["client_order_id"], "bb-test-close")
        self.assertEqual([leg["ratio_qty"] for leg in payload["legs"]], ["1", "1"])
        self.assertEqual(
            {leg["position_intent"] for leg in payload["legs"]},
            {"sell_to_close", "buy_to_close"},
        )

    def test_given_nonunit_leg_totals_when_built_then_ratios_are_relatively_prime(self):
        # Given total close quantities 2 and 4.
        from bookbound.exits import ExitLeg, ExitPlan
        plan = ExitPlan(
            underlying="SPY",
            expiry=FAR_EXPIRY,
            kind="C",
            legs=(
                ExitLeg("SPY260930C00600000", 2, "sell", "sell_to_close"),
                ExitLeg("SPY260930C00605000", 4, "buy", "buy_to_close"),
            ),
            reason="repair",
            detail="flatten malformed ratio",
        )

        # When MLEG quantity and ratios are normalized.
        from bookbound.execute import build_close_payload
        payload = build_close_payload(_priced(plan), client_order_id="bb-test-ratio")

        # Then GCD=2 becomes order qty and the leg ratio is 1:2.
        self.assertEqual(payload["qty"], "2")
        self.assertEqual([leg["ratio_qty"] for leg in payload["legs"]], ["1", "2"])


class DescribeReconciliationPlans(unittest.TestCase):
    def test_given_lone_long_then_it_is_flagged_as_unmanaged_and_flattened(self):
        positions = [option_position("SPY260930C00600000", qty=1)]

        from bookbound.reconcile import build_repair_plans, find_breaches
        breaches = find_breaches(positions)
        plans = build_repair_plans(positions)

        self.assertEqual([item.severity for item in breaches], ["unmanaged"])
        self.assertEqual(plans[0].recommended_action, "flatten")
        self.assertEqual(plans[0].repair, plans[0].flatten)

    def test_given_two_same_series_spreads_then_ambiguous_four_legs_are_unmanaged(self):
        positions = [
            option_position("SPY260930C00600000"),
            option_position("SPY260930C00605000", side="short"),
            option_position("SPY260930C00610000"),
            option_position("SPY260930C00615000", side="short"),
        ]

        from bookbound.reconcile import build_repair_plans
        plan = build_repair_plans(positions)[0]

        self.assertEqual(plan.breach.severity, "unmanaged")
        self.assertEqual(plan.recommended_action, "flatten")
        self.assertEqual(len(plan.flatten.legs), 4)

    def test_given_equity_and_residual_option_then_probable_assignment_halts(self):
        positions = [
            {"asset_class": "us_equity", "symbol": "SPY", "qty": "100",
             "side": "long"},
            option_position("SPY260930C00600000"),
        ]

        from bookbound.reconcile import build_repair_plans, find_breaches
        breaches = find_breaches(positions)

        self.assertEqual([item.severity for item in breaches], ["probable_assignment"])
        self.assertEqual(build_repair_plans(positions), [])

    def test_given_excess_short_fill_when_planned_then_repair_and_flatten_are_explicit(self):
        # Given one long against two shorts: one contract is naked.
        positions = [
            option_position("SPY260930C00600000", qty=1, cost_basis=300),
            option_position(
                "SPY260930C00605000", qty=2, side="short", cost_basis=200,
            ),
        ]

        # When reconciliation plans remediation.
        from bookbound.reconcile import build_repair_plans
        plans = build_repair_plans(positions)

        # Then it offers an immediate balance repair and an atomic full flatten.
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.breach.severity, "naked_short")
        self.assertEqual(plan.recommended_action, "repair_balance")
        self.assertEqual(len(plan.repair.legs), 1)
        self.assertEqual(plan.repair.legs[0].qty, 1)
        self.assertEqual(plan.repair.legs[0].side, "buy")
        self.assertEqual(plan.repair.legs[0].position_intent, "buy_to_close")
        self.assertEqual(
            [(leg.qty, leg.side) for leg in plan.flatten.legs],
            [(1, "sell"), (2, "buy")],
        )

    def test_given_excess_long_fill_when_planned_then_repair_sells_only_excess(self):
        # Given two longs against one short: bounded, but not the intended ratio.
        positions = [
            option_position("SPY260930P00590000", qty=2, cost_basis=400),
            option_position(
                "SPY260930P00595000", qty=1, side="short", cost_basis=100,
            ),
        ]

        # When reconciliation plans remediation.
        from bookbound.reconcile import build_repair_plans
        plans = build_repair_plans(positions)

        # Then one excess long is sold-to-close, preserving a balanced 1:1 spread.
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].breach.severity, "unbalanced")
        self.assertEqual(plans[0].repair.legs[0].qty, 1)
        self.assertEqual(plans[0].repair.legs[0].side, "sell")
        self.assertEqual(
            plans[0].repair.legs[0].position_intent,
            "sell_to_close",
        )

    def test_given_lone_naked_short_when_payload_built_then_safe_single_close_is_explicit(self):
        # Given a short contract with no hedge at all.
        positions = [
            option_position(
                "SPY260930C00605000", qty=2, side="short", cost_basis=200,
            ),
        ]
        from bookbound.reconcile import build_repair_plans
        repair = build_repair_plans(positions)[0].repair

        # When its repair order is built (still no broker call).
        from bookbound.execute import build_close_payload
        payload = build_close_payload(_priced(repair), client_order_id="bb-test-naked")

        # Then it is an unambiguous buy-to-close for the complete naked quantity.
        self.assertNotIn("order_class", payload)
        self.assertEqual(payload["symbol"], "SPY260930C00605000")
        self.assertEqual(payload["qty"], "2")
        self.assertEqual(payload["side"], "buy")
        self.assertEqual(payload["position_intent"], "buy_to_close")


if __name__ == "__main__":
    unittest.main(verbosity=2)
