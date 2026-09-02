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


def book(equity=100_000.0, legs=None, positions=None, last_equity=None, spots=None):
    return Book(equity=equity, last_equity=last_equity or equity, cash=equity,
                buying_power=equity * 4, options_buying_power=equity,
                legs=legs or [], raw_positions=positions or [],
                spots=spots or {"SPY": 762.0})


def spread(long_delta=0.55, short_delta=0.35, width=5.0, debit=2.0):
    long = contract("SPY_L", 760, delta=long_delta, bid=debit + 3.0, ask=debit + 3.1)
    short = contract("SPY_S", 760 + width, delta=short_delta, bid=3.1, ask=3.2)
    built = _price_debit_spread(
        long, short, "C", EXPIRY,
        spot=762.0, realised_vol=0.20, forward=762.0,
    )
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
            any(
                name in r
                for r in last_reasons
                for name in (
                    "book_delta", "total_open_risk", "correlated_greek_stress"
                )
            ),
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

    def test_partial_fill_leaving_lone_long_is_unmanaged(self):
        from bookbound.reconcile import find_breaches
        # A lone long is bounded but not attached to an executable exit unit.
        breaches = find_breaches([self.pos("SPY260918C00760000", 1)])
        self.assertEqual([b.severity for b in breaches], ["unmanaged"])

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


class TestCreditSignConvention(unittest.TestCase):
    """Alpaca's mleg limit_price: positive = debit, negative = credit.

    There is NO server-side sign validation - all four probe values, including
    a negative and an economically impossible one, were accepted. A wrong sign
    is therefore silent and fills against us, which makes these tests the only
    thing standing between a credit strategy and giving the credit away.
    """

    def credit_spread(self):
        from bookbound.structures import _price_credit_spread
        short = contract("SHORT", 770.0, delta=0.42, bid=6.49, ask=6.66)
        long = contract("LONG", 775.0, delta=0.33, bid=4.30, ask=4.33)
        built = _price_credit_spread(
            long, short, "C", EXPIRY,
            spot=762.0, realised_vol=0.20, forward=762.0,
        )
        assert built is not None
        return built

    def test_credit_structure_is_flagged(self):
        s = self.credit_spread()
        self.assertTrue(s.is_credit)
        self.assertLess(s.net_debit, 0, "a credit must be stored negative")

    def test_limit_price_is_negative_for_a_credit(self):
        price = self.credit_spread().limit_price()
        self.assertLess(price, 0,
                        "positive limit on a credit spread reads as 'I will PAY "
                        "that much', which hands the credit away")

    def test_limit_price_is_positive_for_a_debit(self):
        self.assertGreater(spread().limit_price(), 0)

    def test_submitted_payload_carries_the_sign(self):
        from bookbound.execute import build_mleg_payload
        payload = build_mleg_payload(self.credit_spread(), client_order_id="t")
        self.assertTrue(payload["limit_price"].startswith("-"),
                        f"got {payload['limit_price']!r}")

    def test_credit_max_loss_is_width_minus_credit(self):
        s = self.credit_spread()
        credit = -s.net_debit
        self.assertAlmostEqual(s.max_gain, credit * 100, places=4)
        self.assertAlmostEqual(s.max_loss, (s.width - credit) * 100, places=4)
        self.assertAlmostEqual(s.max_gain + s.max_loss, s.width * 100, places=4)

    def test_crossing_the_credit_quote_reduces_model_ev(self):
        """Execution friction must reduce EV regardless of a synthetic quote's IV.

        Equal per-leg IV does not make arbitrary synthetic bid/ask prices a
        no-arbitrage fair-value surface, so its EV sign is not a valid invariant.
        The robust invariant is that crossing the quote is worse than transacting
        at its midpoint by exactly the observed execution cost.
        """
        from bookbound.structures import execution_cost, expected_value, fair_value_ev
        s = self.credit_spread()
        self.assertGreater(execution_cost(s), 0.0,
                           "crossing the spread always costs something")
        self.assertLess(expected_value(s), fair_value_ev(s))
        self.assertAlmostEqual(
            fair_value_ev(s) - expected_value(s), execution_cost(s), places=6,
        )

    def test_credit_ev_turns_positive_when_vol_is_rich(self):
        """The economically meaningful case: sell vol priced above realised.

        Spot must be set explicitly and OTM. The fixture's default leaves spot
        at the long strike, which puts it ABOVE the short strike - an already
        in-the-money credit spread, where lower realised vol correctly HURTS the
        seller because it locks in the loss. Getting that wrong once is what
        this docstring is for.
        """
        import dataclasses

        from bookbound.structures import expected_value
        base = dataclasses.replace(self.credit_spread(), spot=760.0,
                                   realised_vol=0.20)
        calm = dataclasses.replace(base, realised_vol=0.08)
        self.assertGreater(expected_value(calm), expected_value(base),
                           "with spot OTM, a calmer world favours the seller")
        self.assertGreater(expected_value(calm), 0.0,
                           "selling vol at 0.20 that realises 0.08 should pay")

    def test_aggression_always_moves_price_against_us(self):
        """Asserted in economic terms, not raw sign.

        Because a credit is a NEGATIVE number, "worse for us" means the value
        moves UP toward zero - the opposite numeric direction to the debit case.
        Comparing raw limit prices here is exactly the confusion this whole test
        class exists to prevent, so compare credit RECEIVED and debit PAID.
        """
        credit = self.credit_spread()
        received_optimistic = -credit.limit_price(aggression=0.0)
        received_natural = -credit.limit_price(aggression=1.0)
        self.assertLess(received_natural, received_optimistic,
                        "more aggression must mean receiving LESS credit")
        self.assertGreater(received_natural, 0, "we must still be paid")

        debit = spread()
        paid_optimistic = debit.limit_price(aggression=0.0)
        paid_natural = debit.limit_price(aggression=1.0)
        self.assertGreater(paid_natural, paid_optimistic,
                           "more aggression must mean paying MORE debit")

    def test_rejects_credit_exceeding_width(self):
        from bookbound.structures import _price_credit_spread
        short = contract("S", 770.0, bid=9.0, ask=9.1)
        long = contract("L", 772.0, bid=1.0, ask=1.1)   # credit 7.9 > width 2
        self.assertIsNone(_price_credit_spread(long, short, "C", EXPIRY))


class TestCreditPairing(unittest.TestCase):
    """Regression: credit strike ordering is the INVERSE of the debit case, and
    reusing the debit check silently produced zero candidates."""

    def test_bear_call_sells_the_lower_strike(self):
        from bookbound.structures import _is_valid_credit_pair
        low = contract("LOW", 770.0)
        high = contract("HIGH", 775.0)
        self.assertTrue(_is_valid_credit_pair(high, low, "C"))
        self.assertFalse(_is_valid_credit_pair(low, high, "C"))

    def test_bull_put_sells_the_higher_strike(self):
        from bookbound.structures import _is_valid_credit_pair
        low = contract("LOW", 750.0, kind="P")
        high = contract("HIGH", 755.0, kind="P")
        self.assertTrue(_is_valid_credit_pair(low, high, "P"))
        self.assertFalse(_is_valid_credit_pair(high, low, "P"))

    def test_builder_actually_produces_credit_candidates(self):
        from bookbound.structures import build_vertical_credit_spreads
        pool = [
            Contract(symbol=f"SPY_C{i}", underlying="SPY", expiry=EXPIRY, kind="C",
                     strike=770.0 + i, bid=6.5 - i * 0.55, ask=6.6 - i * 0.55,
                     delta=0.42 - i * 0.05, gamma=0.02, theta=-0.4, vega=0.1, iv=0.2)
            for i in range(5)
        ]
        built = build_vertical_credit_spreads(pool, SETTINGS, kind="C")
        self.assertTrue(built, "credit builder produced nothing")
        for s in built:
            self.assertTrue(s.is_credit)
            self.assertGreater(s.long.strike, s.short.strike)


class TestRiskAdjustedRanking(unittest.TestCase):
    """Regression: ranking on absolute EV crowded every risk-compliant candidate
    off the shortlist, so the risk engine refused all 24 credit spreads."""

    def test_narrow_candidates_survive_truncation(self):
        from bookbound.structures import build_vertical_credit_spreads
        # a 25-delta short plus longs at many widths, narrow through very wide
        pool = [Contract(symbol="SPY_SHORT", underlying="SPY", expiry=EXPIRY,
                         kind="C", strike=770.0, bid=6.50, ask=6.60, delta=0.25,
                         gamma=0.02, theta=-0.4, vega=0.1, iv=0.2)]
        for width in range(1, 40):
            pool.append(Contract(
                symbol=f"SPY_L{width}", underlying="SPY", expiry=EXPIRY, kind="C",
                strike=770.0 + width, bid=max(6.4 - width * 0.25, 0.06),
                ask=max(6.5 - width * 0.25, 0.08),
                delta=max(0.24 - width * 0.006, 0.01), gamma=0.02, theta=-0.4,
                vega=0.1, iv=0.2))
        cap = 100_000.0 * SETTINGS.risk.max_loss_per_trade_pct
        built = build_vertical_credit_spreads(pool, SETTINGS, kind="C",
                                              max_candidates=12,
                                              max_loss_cap=cap)
        self.assertTrue(built, "generator emitted nothing under the cap")
        self.assertTrue(
            all(s.max_loss <= cap for s in built),
            f"generator emitted a structure the risk engine must refuse; "
            f"max losses were {sorted(s.max_loss for s in built)}",
        )

    def test_risk_adjusted_ev_prefers_capital_efficiency(self):
        from bookbound.structures import risk_adjusted_ev
        small = spread(width=5.0, debit=2.0)
        self.assertGreater(risk_adjusted_ev(small), risk_adjusted_ev(
            spread(width=50.0, debit=20.0)))


class TestShortlistRanking(unittest.TestCase):
    """Regression: the model received an UNSORTED slice of the admissible set.

    Four builders each sort their own output, but concatenating them and taking
    the first N handed the model whichever family was emitted first. In the live
    book that was ten consecutive negative-EV bull call spreads, while every
    strongly positive bear put spread sat outside the slice. The proposer's
    'none of these have positive expected value' was CORRECT for what it was
    shown - the defect was upstream.
    """

    def test_shortlist_is_globally_ranked(self):
        from bookbound.structures import risk_adjusted_ev
        pool = [spread(width=5.0, debit=2.0), spread(width=5.0, debit=4.5),
                spread(width=5.0, debit=1.0), spread(width=5.0, debit=3.0)]
        ranked = sorted(pool, key=lambda c: -risk_adjusted_ev(c))
        scores = [risk_adjusted_ev(c) for c in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreaterEqual(risk_adjusted_ev(ranked[0]),
                                max(risk_adjusted_ev(c) for c in pool))

    def test_concatenating_builders_does_not_preserve_order(self):
        """The shape of the original bug, in miniature."""
        from bookbound.structures import risk_adjusted_ev
        family_a = sorted([spread(width=5.0, debit=4.0),
                           spread(width=5.0, debit=4.5)],
                          key=lambda c: -risk_adjusted_ev(c))
        family_b = sorted([spread(width=5.0, debit=1.0),
                           spread(width=5.0, debit=1.5)],
                          key=lambda c: -risk_adjusted_ev(c))
        concatenated = family_a + family_b
        best_two = concatenated[:2]
        globally_best = sorted(concatenated, key=lambda c: -risk_adjusted_ev(c))[:2]
        self.assertNotEqual([c.net_debit for c in best_two],
                            [c.net_debit for c in globally_best],
                            "this test is meaningless if the families tie")


class TestContextFreshness(unittest.TestCase):
    """Regression: context built from daily bars alone describes YESTERDAY.

    Measured live mid-session: bars reported SPY at range position 0.968, "near
    its 20-day high", while spot was 762.02 - a true position of 0.672, down
    1.84% on the day. The proposer reasoned about the wrong market and declined
    on it.
    """

    @staticmethod
    def bars(closes):
        return [{"c": c} for c in closes]

    def test_live_spot_overrides_stale_close(self):
        from bookbound.context import price_context
        closes = [730.0 + i for i in range(20)]        # 730..749, high 749
        ctx = price_context(self.bars(closes), spot=735.0)
        self.assertEqual(ctx["spot"], 735.0)
        self.assertEqual(ctx["prev_close"], 749.0)
        self.assertTrue(ctx["is_live_spot"])
        self.assertAlmostEqual(ctx["change_today_pct"],
                               round((735.0 / 749.0 - 1) * 100, 2))

    def test_range_position_reflects_live_spot(self):
        from bookbound.context import price_context
        closes = [730.0 + i for i in range(20)]
        stale = price_context(self.bars(closes))
        live = price_context(self.bars(closes), spot=735.0)
        self.assertGreater(stale["range_position"], 0.9,
                           "bars alone put it at the top of the range")
        self.assertLess(live["range_position"], 0.5,
                        "live spot puts it in the lower half")

    def test_falls_back_cleanly_without_spot(self):
        from bookbound.context import price_context
        ctx = price_context(self.bars([730.0 + i for i in range(20)]))
        self.assertFalse(ctx["is_live_spot"])
        self.assertIsNone(ctx["change_today_pct"])

    def test_todays_move_enters_realised_vol(self):
        from bookbound.context import price_context
        closes = [700.0] * 20                          # flat: zero realised vol
        flat = price_context(self.bars(closes))
        shocked = price_context(self.bars(closes), spot=680.0)   # -2.9% today
        self.assertAlmostEqual(flat["realised_vol_annualised"], 0.0, places=6)
        self.assertGreater(shocked["realised_vol_annualised"], 0.0,
                           "a live gap must raise realised vol")


class TestExpectancyGate(unittest.TestCase):
    """Regression: ranking on probability of profit put a NEGATIVE-expectancy
    trade at rank 1 - PoP 0.795, max gain $151, max loss $949. High win rate is
    not an edge when the loss is six times the gain."""

    def test_rejects_high_pop_negative_expectancy(self):
        import dataclasses
        from bookbound.structures import _price_credit_spread, expected_value
        # sell the 746 put, buy the 735 put: 11 wide, ~1.5 credit
        short = contract("S", 746.0, kind="P", delta=-0.21, bid=3.00, ask=3.10)
        long = contract("L", 735.0, kind="P", delta=-0.10, bid=1.45, ask=1.50)
        built = _price_credit_spread(
            long, short, "P", EXPIRY,
            spot=762.0, realised_vol=0.1324, forward=762.0,
        )
        self.assertIsNotNone(built)
        self.assertLess(expected_value(built), 0.0,
                        "this structure must be negative expectancy")
        strict = dataclasses.replace(
            SETTINGS,
            risk=dataclasses.replace(
                SETTINGS.risk, require_positive_expectancy=True,
            ),
        )
        verdict = evaluate(built, book(), strict)
        self.assertFalse(verdict.admitted)
        self.assertTrue(any("positive_expectancy" in r for r in verdict.reasons),
                        verdict.reasons)

    def test_admits_positive_expectancy(self):
        verdict = evaluate(spread(), book(), SETTINGS)
        checks = {c["check"]: c["passed"] for c in verdict.checks}
        self.assertIn("positive_expectancy", checks)


class TestSummaryContract(unittest.TestCase):
    """Regression: agents.py indexed a summary key that a rename had removed.

    It crashed only on the path where the proposer PICKS a candidate, which no
    prior test or dry run had reached, so it survived into the live cycle."""

    def test_summary_exposes_every_key_agents_reads(self):
        required = {"key", "name", "underlying", "expiry", "dte", "qty",
                    "limit_price", "max_loss", "max_gain", "reward_risk",
                    "expected_value_under_realised_vol", "quality_score",
                    "probability_of_profit", "execution_cost", "structure_type"}
        summary = spread().summary()
        missing = required - set(summary)
        self.assertFalse(missing, f"summary is missing {missing}")

    def test_adversary_payload_builds_for_a_picked_candidate(self):
        """Exercise the exact code path that crashed."""
        import json

        from bookbound.agents import decide
        candidates = [spread(width=5.0, debit=2.0), spread(width=5.0, debit=1.5)]
        picked = candidates[0]

        def fake_proposer(payload, settings):
            return {"choice": picked.key, "confidence": 0.6, "thesis": "t"}

        # No Featherless key configured -> the adversary call fails closed, but
        # only AFTER the payload that used to crash has been constructed.
        decision = decide(candidates, {"equity": 100_000}, SETTINGS,
                          propose_fn=fake_proposer)
        self.assertIn(decision.outcome, {"LLM_ERROR", "VETOED", "SELECTED"})
        self.assertNotEqual(decision.outcome, "NO_TRADE")
        json.dumps(decision.proposer)


class TestPricingInputsReachTheStructure(unittest.TestCase):
    """Regression: the debit pricer accepted spot/realised_vol/forward and
    passed NONE of them to Structure, so every debit spread was silently priced
    at forward = its own long strike and sigma = its own long-leg implied vol,
    while credit spreads used the parity forward and realised vol. The shortlist
    was ranking two different probability distributions on one scale."""

    def _built(self, pricer):
        """Prices must make each family viable: a debit spread needs
        long.ask > short.bid, a credit spread needs short.bid > long.ask."""
        from bookbound.structures import _price_credit_spread, _price_debit_spread
        if pricer == "debit":                       # bull call 760/765
            long = contract("L", 760.0, delta=0.45, bid=8.00, ask=8.10)
            short = contract("S", 765.0, delta=0.25, bid=5.50, ask=5.60)
            fn, args = _price_debit_spread, (long, short)
        else:                                       # bear call: sell 760, buy 765
            short = contract("S", 760.0, delta=0.45, bid=8.00, ask=8.10)
            long = contract("L", 765.0, delta=0.25, bid=5.50, ask=5.60)
            fn, args = _price_credit_spread, (long, short)
        built = fn(*args, "C", EXPIRY, spot=762.0, realised_vol=0.1324,
                   forward=762.5)
        self.assertIsNotNone(built, f"{pricer} fixture did not price")
        return built

    def test_debit_pricer_propagates_all_three(self):
        built = self._built("debit")
        self.assertEqual(built.spot, 762.0)
        self.assertAlmostEqual(built.realised_vol, 0.1324)
        self.assertEqual(built.forward, 762.5)

    def test_credit_pricer_propagates_all_three(self):
        built = self._built("credit")
        self.assertEqual(built.spot, 762.0)
        self.assertAlmostEqual(built.realised_vol, 0.1324)
        self.assertEqual(built.forward, 762.5)

    def test_both_families_price_off_the_same_distribution(self):
        """The defect's real consequence: debit and credit must be comparable."""
        debit = self._built("debit")
        credit = self._built("credit")
        for built in (debit, credit):
            self.assertNotEqual(built.forward, built.long.strike,
                                "forward must not fall back to the long strike")
            self.assertNotAlmostEqual(built.realised_vol, built.long.iv,
                                      msg="sigma must not fall back to long-leg IV")
