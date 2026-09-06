"""Terminal-expiry scenarios for a vertical under an explicit distribution.

Replaces a two-point approximation that used delta as the probability of
finishing ITM. That model had two defects:

1. It priced only the two extremes - max gain and max loss - and assigned ZERO
   value to every outcome between the strikes. Measured on live candidates that
   was 4.6%-10.9% of the probability mass, and the omission grew with width, so
   the ranking was biased toward wider spreads.

2. It was close to circular. Delta IS the risk-neutral probability of finishing
   ITM, so using it as the probability against market prices returns
   approximately zero by construction. Any positive number it produced was an
   artifact, not an edge.

The fix is to integrate the full piecewise-linear payoff against a lognormal
distribution, and to be explicit about WHICH distribution:

* The MARKET's valuation is never modelled. It is the quoted mid of the
  structure, observed directly. Two attempts to model it both manufactured fake
  edge:

    - Pricing each leg at its OWN vendor implied vol. On the live indicative
      feed Alpaca reports put IV exceeding call IV by a uniform 1.61 vol points
      at EVERY strike, which put-call parity forbids. Consuming those leg-by-leg
      injected about a dollar of error per leg, in opposite directions.

    - Pricing both legs at the AVERAGE of their two implied vols. That cannot
      reproduce a skewed market: for a put debit spread the long leg gets a
      too-high vol and the short leg a too-low one, and both errors inflate the
      value in the same direction.

  The lesson generalises: model only what cannot be observed. The mid is
  observable, so observe it.

* The realised-vol calculation is a physical-measure *proxy*, not a calibrated
  forecast.  It describes terminal payoff under one declared lognormal
  scenario.  It does not model the desk's early-exit policy, assignment, jumps,
  skew dynamics, or transaction costs after entry, so it must not be presented
  as the strategy's true expectancy or win probability.

Both are exact closed forms, not simulations: the payoff of a vertical is a
difference of two European options, and the undiscounted expectation of each is
Black-Scholes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TRADING_DAYS = 365.0   # calendar convention, matching DTE


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(forward: float, strike: float, sigma: float, t: float):
    if sigma <= 0 or t <= 0 or forward <= 0 or strike <= 0:
        return None
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * t) / vol_t
    return d1, d1 - vol_t


def expected_call_payoff(forward, strike, sigma, t, rate=0.0) -> float:
    """E[max(S_T - K, 0)] under a lognormal with mean ``forward``.

    Black's formula, undiscounted. The drift is carried by the FORWARD rather
    than a rate parameter, because the forward can be observed directly from
    put-call parity and therefore absorbs the interest rate, the dividend yield,
    and whatever funding convention the data vendor used to compute its greeks.

    Passing a rate is supported for tests that want an explicit drift; it is
    applied to ``forward`` as if it were spot.
    """
    fwd = forward * math.exp(rate * t) if rate else forward
    d = _d1_d2(fwd, strike, sigma, t)
    if d is None:
        return max(fwd - strike, 0.0)
    d1, d2 = d
    return fwd * norm_cdf(d1) - strike * norm_cdf(d2)


def expected_put_payoff(forward, strike, sigma, t, rate=0.0) -> float:
    """E[max(K - S_T, 0)], undiscounted."""
    fwd = forward * math.exp(rate * t) if rate else forward
    d = _d1_d2(fwd, strike, sigma, t)
    if d is None:
        return max(strike - fwd, 0.0)
    d1, d2 = d
    return strike * norm_cdf(-d2) - fwd * norm_cdf(-d1)


def implied_forward(pairs: list[tuple[float, float, float]]) -> float | None:
    """Forward price from put-call parity: F = K + (C - P), median over strikes.

    ``pairs`` is [(strike, call_mid, put_mid), ...] for ONE expiry. The median
    is deliberate: a single crossed or stale quote on the free indicative feed
    would drag a mean badly, and strikes far from the money have the widest
    quotes and the least reliable parity.
    """
    estimates = sorted(k + c - p for k, c, p in pairs if c > 0 and p > 0)
    if not estimates:
        return None
    mid = len(estimates) // 2
    if len(estimates) % 2:
        return estimates[mid]
    return (estimates[mid - 1] + estimates[mid]) / 2.0


def _leg_expectation(kind: str, forward, strike, sigma, t, rate=0.0) -> float:
    return (expected_call_payoff if kind == "C" else expected_put_payoff)(
        forward, strike, sigma, t, rate
    )


@dataclass(frozen=True)
class TerminalScenario:
    """One uncalibrated, policy-unaware terminal-expiry payoff scenario.

    The historical field names remain for source compatibility.  New callers
    should use the horizon-specific nullable properties below: invalid model
    output is unavailable (``None``), not an economic zero.
    """

    ev_at_market: float         # $ - OBSERVED: quoted mid minus our price
    ev_under_rv: float          # $ - MODELLED: realised-vol expectation minus our price
    variance_premium: float     # ev_under_rv - ev_at_market
    market_mid_value: float     # observed mid value of the structure, per share
    spread_value_at_rv: float   # modelled value under realised vol, per share
    probability_of_profit: float
    breakeven: float
    valid: bool
    measure: str = "physical_proxy_lognormal"
    horizon: str = "expiry"
    calibrated: bool = False
    policy_aware: bool = False

    @property
    def expected_pnl_at_expiry(self) -> float | None:
        return self.ev_under_rv if self.valid else None

    @property
    def profit_probability_at_expiry(self) -> float | None:
        return self.probability_of_profit if self.valid else None

    @property
    def breakeven_at_expiry(self) -> float | None:
        return self.breakeven if self.valid else None


# Compatibility name for integrations that imported the original type.
EvBreakdown = TerminalScenario


def spread_terminal_scenario(
    *,
    kind: str,
    long_strike: float,
    short_strike: float,
    net_debit: float,
    spot: float,
    dte: int,
    market_mid_value: float,
    realised_vol: float,
    rate: float = 0.0,
    forward: float | None = None,
    multiplier: int = 100,
) -> TerminalScenario:
    """Integrate terminal payoff against an uncalibrated lognormal proxy.

    Payoff at expiry, valid for calls and puts, debit and credit alike:

        V(S) = intrinsic(long, S) - intrinsic(short, S)
        P&L  = V(S) - net_debit        (net_debit is negative for a credit)

    ``net_debit`` follows the Alpaca mleg convention: positive is paid,
    negative is received.
    """
    t = max(dte, 0) / TRADING_DAYS
    if t <= 0 or spot <= 0 or realised_vol <= 0:
        return TerminalScenario(
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, valid=False
        )
    # Without an observed forward, fall back to spot with whatever drift the
    # caller supplied. That fallback is measurably biased - pricing SPY options
    # at r=0 misvalued calls and puts by up to a dollar in OPPOSITE directions -
    # so callers should supply a parity-implied forward whenever they can.
    fwd = forward if forward and forward > 0 else spot

    # The market's valuation is GIVEN, not derived. No model touches it.
    value_market = market_mid_value
    # Real-world claim: one vol for both legs, so skew is deliberately discarded.
    value_rv = (
        _leg_expectation(kind, fwd, long_strike, realised_vol, t, rate)
        - _leg_expectation(kind, fwd, short_strike, realised_vol, t, rate)
    )

    ev_market = (value_market - net_debit) * multiplier
    ev_rv = (value_rv - net_debit) * multiplier

    # Breakeven: the spot at which V(S) == net_debit. V is monotone between the
    # strikes, so a bisection on that interval is exact enough and cannot diverge.
    lo, hi = sorted((long_strike, short_strike))
    breakeven = _solve_breakeven(kind, long_strike, short_strike, net_debit, lo, hi)
    pop = _probability_of_profit(
        kind, long_strike, short_strike, breakeven, fwd, realised_vol, t, rate
    )

    return TerminalScenario(
        ev_at_market=ev_market,
        ev_under_rv=ev_rv,
        variance_premium=ev_rv - ev_market,
        market_mid_value=value_market,
        spread_value_at_rv=value_rv,
        probability_of_profit=pop,
        breakeven=breakeven,
        valid=True,
    )


def spread_expected_value(**kwargs) -> TerminalScenario:
    """Compatibility wrapper for :func:`spread_terminal_scenario`.

    The old name is intentionally not used by shortlist or risk code because it
    omits both the terminal horizon and the scenario's calibration limitations.
    """
    return spread_terminal_scenario(**kwargs)


def payoff(kind: str, long_strike: float, short_strike: float,
           net_debit: float, spot_at_expiry: float) -> float:
    """P&L per share at expiry. Exposed so tests can assert the payoff directly."""
    if kind == "C":
        intrinsic = (max(spot_at_expiry - long_strike, 0.0)
                     - max(spot_at_expiry - short_strike, 0.0))
    else:
        intrinsic = (max(long_strike - spot_at_expiry, 0.0)
                     - max(short_strike - spot_at_expiry, 0.0))
    return intrinsic - net_debit


def _solve_breakeven(kind, long_strike, short_strike, net_debit, lo, hi) -> float:
    """Bisect for P&L == 0 between the strikes."""
    f_lo = payoff(kind, long_strike, short_strike, net_debit, lo)
    f_hi = payoff(kind, long_strike, short_strike, net_debit, hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        # profit or loss across the whole interval; no interior breakeven
        return lo if abs(f_lo) < abs(f_hi) else hi
    for _ in range(60):
        mid = (lo + hi) / 2.0
        f_mid = payoff(kind, long_strike, short_strike, net_debit, mid)
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _probability_of_profit(kind, long_strike, short_strike, breakeven,
                           forward, sigma, t, rate) -> float:
    """P(P&L > 0) under the realised-vol lognormal."""
    d = _d1_d2(forward * math.exp(rate * t) if rate else forward,
               breakeven, sigma, t)
    if d is None:
        return 0.0
    _, d2 = d
    profit_above = payoff(kind, long_strike, short_strike,
                          0.0, max(long_strike, short_strike) * 2) > payoff(
        kind, long_strike, short_strike, 0.0, 0.0)
    # N(d2) is P(S_T > breakeven)
    return norm_cdf(d2) if profit_above else norm_cdf(-d2)
