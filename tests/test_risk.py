"""Unit tests for the deterministic layers.

These run without network access or credentials - the whole point of a
deterministic risk engine is that it is testable in isolation.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.book import Book, Leg, greeks_of_legs           # noqa: E402
from bookbound.config import RiskBudget, Settings              # noqa: E402
from bookbound.market import Contract, parse_occ, tradeable    # noqa: E402
from bookbound.risk import evaluate, shrink_to_fit             # noqa: E402
from bookbound.structures import _price_debit_spread           # noqa: E402

SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())
EXPIRY = date.today() + timedelta(days=21)


def contract(symbol, strike, kind="C", delta=0.5, bid=1.00, ask=1.10,
             gamma=0.02, theta=-0.5, vega=0.10, iv=0.20):
    return Contract(symbol=symbol, underlying="SPY", expiry=EXPIRY, kind=kind,
                    strike=strike, bid=bid, ask=ask, delta=delta, gamma=gamma,
                    theta=theta, vega=vega, iv=iv)


def book(equity=100_000.0, legs=None, positions=None, last_equity=None):
    return Book(equity=equity, last_equity=last_equity or equity, cash=equity,
                buying_power=equity * 4, options_buying_power=equity,
                legs=legs or [], raw_positions=positions or [])


def spread(long_delta=0.55, short_delta=0.35, width=5.0, debit=2.0):
    long = contract("SPY_L", 760, delta=long_delta, bid=debit + 3.0, ask=debit + 3.1)
    short = contract("SPY_S", 760 + width, delta=short_delta, bid=3.1, ask=3.2)
    built = _price_debit_spread(long, short, "C", EXPIRY)
    assert built is not None
    return built


class TestGreekAggregation(unittest.TestCase):
    def test_short_leg_flips_every_greek(self):
        long = greeks_of_legs([Leg("A", 1, 0.30, 0.02, -0.50, 0.12)])
        short = greeks_of_legs([Leg("A", -1, 0.30, 0.02, -0.50, 0.12)])
        self.assertAlmostEqual(long.delta, -short.delta)
        self.assertAlmostEqual(long.theta, -short.theta)
        self.assertGreater(short.theta, 0, "short premium collects theta")

    def test_scaled_by_contract_multiplier(self):
        g = greeks_of_legs([Leg("A", 2, 0.50, 0.0, 0.0, 0.0)])
        self.assertAlmostEqual(g.delta, 0.50 * 2 * 100)


class TestPortfolioGates(unittest.TestCase):
    def test_admits_a_reasonable_first_trade(self):
        verdict = evaluate(spread(), book(), SETTINGS)
        self.assertTrue(verdict.admitted, verdict.reasons)

    def test_rejects_when_book_delta_would_breach(self):
        """The case per-trade gating misses: each spread is fine, the book is not."""
        existing = [Leg(f"X{i}", 1, 0.40, 0.01, -0.3, 0.1) for i in range(4)]
        b = book(legs=existing)                      # 4 * 0.40 * 100 = 160 delta
        self.assertGreater(abs(b.greeks.delta), SETTINGS.risk.max_abs_delta)
        verdict = evaluate(spread(), b, SETTINGS)
        self.assertFalse(verdict.admitted)
        self.assertTrue(any("book_delta" in r for r in verdict.reasons), verdict.reasons)

    def test_individually_safe_trades_are_collectively_refused(self):
        """The failure mode per-trade gating misses.

        Every one of these spreads passes its own max-loss check. Repeated, they
        accumulate directional delta until the BOOK gate stops them. A per-trade
        gate would have admitted all 20.
        """
        b = book()
        pending = 0.0
        admitted = 0
        last_reasons: list[str] = []
        for _ in range(20):
            candidate = spread()
            self.assertLessEqual(candidate.max_loss,
                                 b.equity * SETTINGS.risk.max_loss_per_trade_pct,
                                 "each trade is individually within its own cap")
            verdict = evaluate(candidate, b, SETTINGS, pending_risk=pending)
            if not verdict.admitted:
                last_reasons = verdict.reasons
                break
            admitted += 1
            pending += candidate.max_loss
            b = b.with_added(candidate.legs)
        self.assertGreater(admitted, 0, "should admit at least one")
        self.assertLess(admitted, 20, "must refuse before the book runs away")
        self.assertTrue(
            any("book_delta" in r or "total_open_risk" in r for r in last_reasons),
            f"a PORTFOLIO gate must be what stops it, got: {last_reasons}",
        )

    def test_pending_risk_is_counted_within_a_cycle(self):
        """Risk committed earlier in the same cycle must not be invisible."""
        cap = 100_000.0 * SETTINGS.risk.max_total_open_risk_pct
        verdict = evaluate(spread(), book(), SETTINGS, pending_risk=cap)
        self.assertFalse(verdict.admitted)
        self.assertTrue(any("total_open_risk" in r for r in verdict.reasons))

    def test_drawdown_halt_blocks_everything(self):
        b = book(equity=96_000.0, last_equity=100_000.0)   # -4%
        verdict = evaluate(spread(), b, SETTINGS)
        self.assertFalse(verdict.admitted)
        self.assertTrue(any("daily_drawdown_halt" in r for r in verdict.reasons))

    def test_fails_closed_on_unpriced_positions(self):
        verdict = evaluate(spread(), book(), SETTINGS,
                           unpriced_positions=["SPY260918C00700000"])
        self.assertFalse(verdict.admitted)
        self.assertTrue(any("book_fully_priced" in r for r in verdict.reasons))

    def test_per_trade_loss_cap(self):
        big = spread(width=50.0, debit=20.0)       # $2,000 loss vs $1,000 cap
        verdict = evaluate(big, book(), SETTINGS)
        self.assertFalse(verdict.admitted)
        self.assertTrue(any("max_loss_per_trade" in r for r in verdict.reasons))

    def test_every_rejection_names_its_bound(self):
        verdict = evaluate(spread(width=50.0, debit=20.0), book(), SETTINGS)
        failed = [c for c in verdict.checks if not c["passed"]]
        self.assertTrue(failed)
        for check in failed:
            self.assertTrue(check["detail"], "a refusal must explain itself")

    def test_shrink_never_enlarges(self):
        result = shrink_to_fit(spread().at_qty(10), book(), SETTINGS)
        if result is not None:
            self.assertLessEqual(result.qty, 10)


class TestStructurePricing(unittest.TestCase):
    def test_rejects_spread_costing_more_than_its_width(self):
        long = contract("L", 760, bid=9.0, ask=9.5)
        short = contract("S", 762, bid=1.0, ask=1.1)      # debit 8.5 > width 2
        self.assertIsNone(_price_debit_spread(long, short, "C", EXPIRY))

    def test_max_loss_equals_debit_paid(self):
        s = spread(width=5.0, debit=2.0)
        self.assertAlmostEqual(s.max_loss, s.net_debit * 100)
        self.assertAlmostEqual(s.max_gain + s.max_loss, s.width * 100)

    def test_limit_price_never_below_mid(self):
        s = spread()
        mid = s.long.mid - s.short.mid
        self.assertGreaterEqual(s.limit_price(aggression=0.5), round(mid, 2) - 0.01)


class TestMarketDefences(unittest.TestCase):
    def test_occ_roundtrip(self):
        self.assertEqual(parse_occ("SPY260918P00755000"),
                         ("SPY", date(2026, 9, 18), "P", 755.0))

    def test_wide_quote_is_untradeable(self):
        wide = contract("W", 760, bid=5.63, ask=6.83)     # the measured ~19% spread
        self.assertFalse(tradeable(wide, SETTINGS))

    def test_penny_bid_is_untradeable(self):
        self.assertFalse(tradeable(contract("P", 760, bid=0.01, ask=0.02), SETTINGS))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReconciliation(unittest.TestCase):
    """A half-filled spread is the failure mode paper trading injects on purpose."""

    @staticmethod
    def pos(symbol, qty, side="long"):
        return {"asset_class": "us_option", "symbol": symbol,
                "qty": str(abs(qty)), "side": side, "cost_basis": "200"}

    def test_balanced_vertical_is_healthy(self):
        from bookbound.reconcile import find_breaches
        self.assertEqual(find_breaches([
            self.pos("SPY260918C00760000", 1),
            self.pos("SPY260918C00765000", 1, "short"),
        ]), [])

    def test_partial_fill_leaving_naked_short_is_caught(self):
        from bookbound.reconcile import find_breaches
        breaches = find_breaches([self.pos("SPY260918C00765000", 1, "short")])
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0].severity, "naked_short")

    def test_partial_fill_leaving_lone_long_is_not_urgent(self):
        from bookbound.reconcile import find_breaches
        # a lone long is defined-risk; it is not a naked short
        breaches = find_breaches([self.pos("SPY260918C00760000", 1)])
        self.assertEqual([b.severity for b in breaches], [])

    def test_unbalanced_ratio_is_flagged(self):
        from bookbound.reconcile import find_breaches
        breaches = find_breaches([
            self.pos("SPY260918C00760000", 2),
            self.pos("SPY260918C00765000", 1, "short"),
        ])
        self.assertEqual([b.severity for b in breaches], ["unbalanced"])


class TestVerticalIntegrity(unittest.TestCase):
    """Regression: legs from different underlyings were being paired as a
    'vertical', producing nonsense widths and fantasy reward/risk."""

    def test_refuses_cross_underlying_pair(self):
        from bookbound.market import Contract as C
        iwm = C(symbol="IWM260909C00295000", underlying="IWM", expiry=EXPIRY,
                kind="C", strike=295.0, bid=1.0, ask=1.05, delta=0.5, gamma=0.02,
                theta=-0.5, vega=0.1, iv=0.2)
        spy = contract("SPY260909C00773000", 773.0)
        self.assertIsNone(_price_debit_spread(iwm, spy, "C", EXPIRY))

    def test_refuses_cross_expiry_pair(self):
        long = contract("A", 760)
        far = Contract(symbol="B", underlying="SPY",
                       expiry=EXPIRY + timedelta(days=30), kind="C", strike=765,
                       bid=1.0, ask=1.1, delta=0.4, gamma=0.02, theta=-0.4,
                       vega=0.1, iv=0.2)
        self.assertIsNone(_price_debit_spread(long, far, "C", EXPIRY))

    def test_built_spreads_are_all_true_verticals(self):
        from bookbound.structures import build_vertical_debit_spreads
        pool = []
        for underlying, base in (("SPY", 760.0), ("IWM", 295.0)):
            for offset in range(0, 6):
                pool.append(Contract(
                    symbol=f"{underlying}_C{offset}", underlying=underlying,
                    expiry=EXPIRY, kind="C", strike=base + offset,
                    bid=5.0 - offset * 0.7, ask=5.1 - offset * 0.7,
                    delta=0.6 - offset * 0.08, gamma=0.02, theta=-0.4,
                    vega=0.1, iv=0.2))
        built = build_vertical_debit_spreads(pool, SETTINGS, kind="C",
                                             max_candidates=50)
        self.assertTrue(built, "expected some candidates")
        for s in built:
            self.assertEqual(s.long.underlying, s.short.underlying)
            self.assertEqual(s.long.expiry, s.short.expiry)
            self.assertLess(s.width, 10.0, f"implausible width: {s.width}")


class TestExits(unittest.TestCase):
    """Entries without exits is not a strategy."""

    @staticmethod
    def pos(symbol, qty=1, side="long", basis=200.0, upl=0.0):
        return {"asset_class": "us_option", "symbol": symbol, "qty": str(qty),
                "side": side, "cost_basis": str(basis), "unrealized_pl": str(upl)}

    def far_symbol(self):
        far = date.today() + timedelta(days=30)
        return f"SPY{far.strftime('%y%m%d')}C00760000"

    def near_symbol(self):
        near = date.today() + timedelta(days=1)
        return f"SPY{near.strftime('%y%m%d')}C00760000"

    def test_profit_target_closes_a_long(self):
        from bookbound.exits import evaluate_exits
        out = evaluate_exits([self.pos(self.far_symbol(), upl=120.0)], {}, SETTINGS)
        self.assertEqual([o.reason for o in out], ["profit_target"])
        self.assertEqual(out[0].side, "sell")
        self.assertEqual(out[0].position_intent, "sell_to_close")

    def test_stop_loss_closes_before_max_loss(self):
        from bookbound.exits import evaluate_exits
        out = evaluate_exits([self.pos(self.far_symbol(), upl=-140.0)], {}, SETTINGS)
        self.assertEqual([o.reason for o in out], ["stop_loss"])

    def test_time_stop_overrides_a_winning_position(self):
        from bookbound.exits import evaluate_exits
        out = evaluate_exits([self.pos(self.near_symbol(), upl=130.0)], {}, SETTINGS)
        self.assertEqual([o.reason for o in out], ["time_stop"],
                         "the calendar rule is absolute")

    def test_short_leg_closes_with_a_buy(self):
        from bookbound.exits import evaluate_exits
        out = evaluate_exits(
            [self.pos(self.far_symbol(), side="short", upl=120.0)], {}, SETTINGS)
        self.assertEqual(out[0].side, "buy")
        self.assertEqual(out[0].position_intent, "buy_to_close")

    def test_untouched_position_is_left_alone(self):
        from bookbound.exits import evaluate_exits
        self.assertEqual(
            evaluate_exits([self.pos(self.far_symbol(), upl=10.0)], {}, SETTINGS), [])

    def test_flatten_closes_everything(self):
        from bookbound.exits import deadline_flatten
        out = deadline_flatten([self.pos(self.far_symbol(), upl=10.0)], "deadline")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reason, "flatten")
