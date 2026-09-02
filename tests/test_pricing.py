"""Tests for the distribution-integrated EV model.

The closed form is cross-checked against Monte Carlo. A pricing model nobody
verified independently is exactly the kind of thing that produces confident
wrong numbers.
"""
from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.pricing import (  # noqa: E402  # noqa: E402
    expected_call_payoff,
    expected_put_payoff,
    norm_cdf,
    payoff,
    spread_expected_value,
)


def fair_mid(kind, long_k, short_k, spot, sigma, t, rate=0.0):
    """The structure's value under a single lognormal - a stand-in for an
    observed market mid in tests where we want a KNOWN fair price."""
    fn = expected_call_payoff if kind == "C" else expected_put_payoff
    return (fn(spot, long_k, sigma, t, rate) - fn(spot, short_k, sigma, t, rate))


def mc_expected_payoff(kind, long_k, short_k, net_debit, spot, sigma, t,
                       rate=0.0, n=200_000, seed=7):
    rng = random.Random(seed)
    total = 0.0
    drift = (rate - 0.5 * sigma * sigma) * t
    vol = sigma * math.sqrt(t)
    for _ in range(n):
        s_t = spot * math.exp(drift + vol * rng.gauss(0.0, 1.0))
        total += payoff(kind, long_k, short_k, net_debit, s_t)
    return total / n


class TestPayoff(unittest.TestCase):
    def test_bull_call_debit(self):
        f = lambda s: payoff("C", 760, 765, 2.0, s)   # noqa: E731
        self.assertAlmostEqual(f(700), -2.0)
        self.assertAlmostEqual(f(762), 0.0)
        self.assertAlmostEqual(f(800), 3.0)

    def test_bear_put_debit(self):
        f = lambda s: payoff("P", 765, 760, 2.0, s)   # noqa: E731
        self.assertAlmostEqual(f(800), -2.0)
        self.assertAlmostEqual(f(763), 0.0)
        self.assertAlmostEqual(f(700), 3.0)

    def test_bear_call_credit(self):
        # short 760 call, long 765 call, credit 2.00 -> net_debit -2.00
        f = lambda s: payoff("C", 765, 760, -2.0, s)  # noqa: E731
        self.assertAlmostEqual(f(700), 2.0, msg="keep the credit")
        self.assertAlmostEqual(f(800), -3.0, msg="width minus credit")

    def test_bull_put_credit(self):
        # short 765 put, long 760 put, credit 2.00
        f = lambda s: payoff("P", 760, 765, -2.0, s)  # noqa: E731
        self.assertAlmostEqual(f(800), 2.0)
        self.assertAlmostEqual(f(700), -3.0)

    def test_max_gain_plus_max_loss_equals_width(self):
        for kind, lk, sk in (("C", 760, 765), ("P", 765, 760)):
            best = payoff(kind, lk, sk, 2.0, 10_000 if kind == "C" else 0.0)
            worst = payoff(kind, lk, sk, 2.0, 0.0 if kind == "C" else 10_000)
            self.assertAlmostEqual(best - worst, abs(lk - sk))


class TestClosedFormAgainstMonteCarlo(unittest.TestCase):
    """The load-bearing check: does the analytic expectation match simulation?"""

    def test_call_expectation(self):
        analytic = expected_call_payoff(760, 765, 0.18, 30 / 365, 0.04)
        mc = mc_expected_payoff("C", 765, 10_000_000, 0.0, 760, 0.18, 30 / 365, 0.04)
        self.assertAlmostEqual(analytic, mc, delta=0.15)

    def test_put_expectation(self):
        analytic = expected_put_payoff(760, 755, 0.18, 30 / 365, 0.04)
        mc = mc_expected_payoff("P", 755, 0.0, 0.0, 760, 0.18, 30 / 365, 0.04)
        self.assertAlmostEqual(analytic, mc, delta=0.15)

    def test_spread_ev_matches_simulation(self):
        spot, sigma, t, rate = 760.0, 0.18, 30 / 365, 0.04
        res = spread_expected_value(
            kind="C", long_strike=760, short_strike=765, net_debit=2.0,
            spot=spot, dte=30,
            market_mid_value=fair_mid("C", 760, 765, spot, sigma, t, rate),
            realised_vol=sigma, rate=rate,
        )
        mc = mc_expected_payoff("C", 760, 765, 2.0, spot, sigma, t, rate) * 100
        self.assertAlmostEqual(res.ev_under_rv, mc, delta=15.0)

    def test_credit_spread_ev_matches_simulation(self):
        spot, sigma, rate = 760.0, 0.18, 0.04
        res = spread_expected_value(
            kind="C", long_strike=765, short_strike=760, net_debit=-2.0,
            spot=spot, dte=30,
            market_mid_value=fair_mid("C", 765, 760, spot, sigma, 30 / 365, rate),
            realised_vol=sigma, rate=rate,
        )
        mc = mc_expected_payoff("C", 765, 760, -2.0, spot, sigma, 30 / 365, rate) * 100
        self.assertAlmostEqual(res.ev_under_rv, mc, delta=15.0)


class TestNoFreeLunch(unittest.TestCase):
    """A spread bought at its own model value must have ~zero EV under that
    same model. If this fails, the model is manufacturing edge from nothing."""

    def test_ev_at_fair_value_is_zero(self):
        spot, sigma = 760.0, 0.18
        fair = (expected_call_payoff(spot, 760, sigma, 30 / 365, 0.0)
                - expected_call_payoff(spot, 765, sigma, 30 / 365, 0.0))
        res = spread_expected_value(
            kind="C", long_strike=760, short_strike=765, net_debit=fair,
            spot=spot, dte=30, market_mid_value=fair,
            realised_vol=sigma, rate=0.0,
        )
        self.assertAlmostEqual(res.ev_at_market, 0.0, delta=0.01)
        self.assertAlmostEqual(res.ev_under_rv, 0.0, delta=0.01)

    def test_paying_above_fair_value_is_negative_ev(self):
        spot, sigma = 760.0, 0.18
        fair = (expected_call_payoff(spot, 760, sigma, 30 / 365, 0.0)
                - expected_call_payoff(spot, 765, sigma, 30 / 365, 0.0))
        res = spread_expected_value(
            kind="C", long_strike=760, short_strike=765, net_debit=fair + 0.20,
            spot=spot, dte=30, market_mid_value=fair,
            realised_vol=sigma, rate=0.0,
        )
        self.assertAlmostEqual(res.ev_at_market, -20.0, delta=0.5,
                               msg="overpaying by 0.20 costs exactly $20")

    def test_variance_premium_has_the_right_sign(self):
        """IV above RV should make SELLING premium the positive-EV side."""
        spot = 760.0
        credit = spread_expected_value(
            kind="C", long_strike=765, short_strike=760, net_debit=-1.5,
            spot=spot, dte=30,
            market_mid_value=fair_mid("C", 765, 760, spot, 0.25, 30 / 365),
            realised_vol=0.12, rate=0.0,
        )
        self.assertGreater(credit.variance_premium, 0,
                           "selling rich vol must gain under a calmer real world")
        debit = spread_expected_value(
            kind="C", long_strike=760, short_strike=765, net_debit=1.5,
            spot=spot, dte=30,
            market_mid_value=fair_mid("C", 760, 765, spot, 0.25, 30 / 365),
            realised_vol=0.12, rate=0.0,
        )
        self.assertLess(debit.variance_premium, 0,
                        "buying rich vol must lose under a calmer real world")


class TestProbabilityOfProfit(unittest.TestCase):
    def test_breakeven_is_between_the_strikes(self):
        res = spread_expected_value(
            kind="C", long_strike=760, short_strike=765, net_debit=2.0,
            spot=760, dte=30,
            market_mid_value=fair_mid("C", 760, 765, 760, 0.18, 30 / 365),
            realised_vol=0.18,
        )
        self.assertAlmostEqual(res.breakeven, 762.0, delta=0.01)

    def test_pop_between_zero_and_one(self):
        res = spread_expected_value(
            kind="P", long_strike=765, short_strike=760, net_debit=2.0,
            spot=762, dte=30,
            market_mid_value=fair_mid("P", 765, 760, 762, 0.18, 30 / 365),
            realised_vol=0.18,
        )
        self.assertTrue(0.0 <= res.probability_of_profit <= 1.0)

    def test_deep_otm_debit_has_low_pop(self):
        far = spread_expected_value(
            kind="C", long_strike=800, short_strike=805, net_debit=0.5,
            spot=760, dte=7,
            market_mid_value=fair_mid("C", 800, 805, 760, 0.15, 7 / 365),
            realised_vol=0.15,
        )
        self.assertLess(far.probability_of_profit, 0.15)


class TestNormCdf(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(norm_cdf(0.0), 0.5, places=9)
        self.assertAlmostEqual(norm_cdf(1.96), 0.975, places=3)
        self.assertAlmostEqual(norm_cdf(-1.96), 0.025, places=3)


if __name__ == "__main__":
    unittest.main()
