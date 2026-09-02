"""Build and price candidate option structures.

Deterministic. Runs BEFORE any model does, so the shortlist a model sees
contains only structures that are real, priced, and tradeable. A model cannot
express an untradeable idea because untradeable ideas are not in its input.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from .book import CONTRACT_MULTIPLIER, Leg
from .config import Settings
from .market import Contract, tradeable
from .pricing import EvBreakdown, implied_forward, spread_expected_value


@dataclass(frozen=True)
class Structure:
    """A defined-risk multi-leg options structure, priced from real quotes."""

    name: str
    underlying: str
    expiry: date
    qty: int
    long: Contract
    short: Contract
    net_debit: float          # per spread, per share. POSITIVE = we pay a debit,
                              # NEGATIVE = we receive a credit. This mirrors
                              # Alpaca's mleg limit_price convention exactly, so
                              # the sign never has to be flipped at the boundary.
    max_loss: float           # dollars, total for qty
    max_gain: float           # dollars, total for qty
    spot: float = 0.0         # underlying at construction, for the EV integral
    realised_vol: float = 0.0 # real-world vol assumption for the EV integral
    forward: float = 0.0      # parity-implied forward for this expiry
    rationale: str = ""

    @property
    def is_credit(self) -> bool:
        return self.net_debit < 0

    @property
    def dte(self) -> int:
        return (self.expiry - date.today()).days

    def ev(self, net_debit: float | None = None) -> EvBreakdown:
        """Full-distribution expected value at a given entry price."""
        return spread_expected_value(
            kind=self.long.kind,
            long_strike=self.long.strike,
            short_strike=self.short.strike,
            net_debit=self.net_debit if net_debit is None else net_debit,
            spot=self.spot or self.long.strike,
            dte=self.dte,
            market_mid_value=self.long.mid - self.short.mid,
            realised_vol=self.realised_vol or self.long.iv,
            forward=self.forward or None,
            multiplier=CONTRACT_MULTIPLIER * self.qty,
        )

    @property
    def width(self) -> float:
        return abs(self.long.strike - self.short.strike)

    @property
    def legs(self) -> list[Leg]:
        return [
            Leg.from_contract(self.long, self.qty),
            Leg.from_contract(self.short, -self.qty),
        ]

    @property
    def key(self) -> str:
        return f"{self.name}:{self.long.symbol}/{self.short.symbol}"

    def at_qty(self, qty: int) -> "Structure":
        scale = qty / self.qty if self.qty else 0
        return replace(
            self, qty=qty, max_loss=self.max_loss * scale, max_gain=self.max_gain * scale
        )

    def limit_price(self, *, aggression: float = 0.5) -> float:
        """Limit price in Alpaca's mleg convention.

        Per the Trading API reference and the alpaca-py source:
          "A positive value indicates a debit, representing a cost or payment to
           be made. A negative value signifies a credit, reflecting an amount to
           be received."

        There is NO server-side sign validation - a wrong sign is accepted
        silently and fills against you. A credit spread submitted at +0.01 reads
        as "I will pay up to a 1c debit", which a structure worth 2.16 of credit
        satisfies immediately, handing the credit away.

        Both directions cross the spread, so ``aggression`` always moves the
        price AGAINST us: 0.0 = optimistic mid, 1.0 = the natural price.
        """
        natural = self.long.ask - self.short.bid   # pay more / receive less
        mid = self.long.mid - self.short.mid
        price = mid + (natural - mid) * aggression
        # keep the sign; only clamp the magnitude away from zero
        if price < 0:
            return min(round(price, 2), -0.01)
        return max(round(price, 2), 0.01)

    def summary(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "dte": (self.expiry - date.today()).days,
            "qty": self.qty,
            "long": self.long.symbol,
            "short": self.short.symbol,
            "width": self.width,
            "net_debit": round(self.net_debit, 2),
            "structure_type": "credit" if self.is_credit else "debit",
            "limit_price": self.limit_price(),
            "max_loss": round(self.max_loss, 2),
            "max_gain": round(self.max_gain, 2),
            "reward_risk": round(self.max_gain / self.max_loss, 2) if self.max_loss else 0,
            "expected_value_under_realised_vol": round(expected_value(self), 2),
            "ev_per_dollar_risked": round(risk_adjusted_ev(self), 4),
            "quality_score": round(quality_score(self), 4),
            "execution_cost": round(execution_cost(self), 2),
            "variance_premium": round(variance_premium(self), 2),
            "probability_of_profit": round(probability_of_profit(self), 4),
            "execution_drag": round(execution_drag(self), 2),
            "long_delta": round(self.long.delta, 4),
            "short_delta": round(self.short.delta, 4),
            "long_iv": round(self.long.iv, 4),
            "short_iv": round(self.short.iv, 4),
            "rationale": self.rationale,
        }


def forwards_by_series(contracts: list[Contract]) -> dict[tuple[str, date], float]:
    """Parity-implied forward per (underlying, expiry).

    Pricing options off raw spot with a guessed interest rate was measurably
    wrong: SPY calls and puts were misvalued by up to a dollar in OPPOSITE
    directions, which is the signature of a missing forward drift and which made
    edge-versus-mid come out spuriously POSITIVE. The forward is observable, so
    observe it rather than guessing its components.
    """
    calls: dict[tuple[str, date, float], float] = {}
    puts: dict[tuple[str, date, float], float] = {}
    for c in contracts:
        target = calls if c.kind == "C" else puts
        target[(c.underlying, c.expiry, c.strike)] = c.mid

    grouped: dict[tuple[str, date], list[tuple[float, float, float]]] = {}
    for (underlying, expiry, strike), call_mid in calls.items():
        put_mid = puts.get((underlying, expiry, strike))
        if put_mid is None:
            continue
        grouped.setdefault((underlying, expiry), []).append(
            (strike, call_mid, put_mid)
        )

    out: dict[tuple[str, date], float] = {}
    for series, pairs in grouped.items():
        fwd = implied_forward(pairs)
        if fwd:
            out[series] = fwd
    return out


def build_vertical_debit_spreads(
    contracts: list[Contract],
    settings: Settings,
    *,
    kind: str,
    target_delta: float = 0.45,
    max_candidates: int = 12,
    spot: float | None = None,
    max_loss_cap: float | None = None,
    realised_vols: dict[str, float] | None = None,
) -> list[Structure]:
    """Construct defined-risk vertical DEBIT spreads.

    Debit rather than credit, deliberately. The sign convention for MLEG
    ``limit_price`` on net-credit structures is not yet verified against the
    live API (knowledge-base/10-verification-log.md); a debit spread's limit is
    unambiguous - it is the most we will pay. Max loss is the debit, known
    exactly at entry, with no assignment risk.

    ``kind`` "C" builds bull call spreads, "P" builds bear put spreads.
    """
    budget = settings.risk
    forwards = forwards_by_series(contracts)
    usable = [
        c for c in contracts
        if c.kind == kind and tradeable(c, settings)
    ]
    if len(usable) < 2:
        return []

    # Group by (underlying, expiry). Grouping by expiry alone silently pairs
    # legs from DIFFERENT underlyings - e.g. long IWM 295 against short SPY 773 -
    # which is not a vertical, cannot be filled as one, and prices as nonsense.
    by_series: dict[tuple[str, date], list[Contract]] = {}
    for contract in usable:
        by_series.setdefault((contract.underlying, contract.expiry), []).append(contract)

    candidates: list[Structure] = []
    for (_underlying, expiry), group in by_series.items():
        group.sort(key=lambda c: c.strike)
        # long leg: closest to the target delta
        anchor = min(group, key=lambda c: abs(abs(c.delta) - target_delta))
        for short in group:
            if not _is_valid_pair(anchor, short, kind):
                continue
            # The short leg must carry real premium. Selling a 0.02-delta wing
            # finances nothing: the structure is a long call wearing a costume,
            # and it consumes the delta budget like one.
            if not (budget.min_short_delta <= abs(short.delta) <= budget.max_short_delta):
                continue
            reference = spot or anchor.strike
            if abs(anchor.strike - short.strike) > reference * budget.max_width_pct_of_spot:
                continue
            structure = _price_debit_spread(
                anchor, short, kind, expiry, spot=spot,
                realised_vol=(realised_vols or {}).get(anchor.underlying, 0.0),
                forward=forwards.get((anchor.underlying, expiry), 0.0))
            if structure is not None:
                candidates.append(structure)

    # Enforce the per-trade loss cap HERE, not downstream. Ranking cannot be
    # relied on to preserve compliant candidates: sorting by EV (absolute or
    # risk-adjusted) favours wide, high-notional spreads, which crowded every
    # affordable candidate off a truncated shortlist. The generator must not
    # emit what the risk engine will certainly refuse.
    if max_loss_cap is not None:
        candidates = [c for c in candidates if c.max_loss <= max_loss_cap]

    candidates.sort(key=lambda s: -quality_score(s))
    return candidates[:max_candidates]


def expected_value(structure: Structure) -> float:
    """Expected value in dollars under the REALISED-vol distribution.

    This is the real-world claim: what the structure is worth if the underlying
    behaves like its recent realised volatility rather than like the implied
    surface. It integrates the entire piecewise-linear payoff, not just the two
    extremes.
    """
    result = structure.ev()
    return result.ev_under_rv if result.valid else 0.0


def execution_cost(structure: Structure) -> float:
    """What crossing the spread costs us, in dollars. OBSERVED, not modelled.

    This used to be derived from Black-Scholes at the vendor's per-leg implied
    vols, which was both unnecessary and wrong: the vendor's IVs violate
    put-call parity by a uniform 1.6 vol points, so the "model" invented edge.

    Our execution price and the quoted mid are both directly observable, so
    subtract them. No model can improve on that, and none can corrupt it.
    """
    mid_debit = structure.long.mid - structure.short.mid
    return abs(structure.net_debit - mid_debit) * CONTRACT_MULTIPLIER * structure.qty


def variance_premium(structure: Structure) -> float:
    """Modelled value under realised vol MINUS the observed market mid.

    ⚠️ THIS IS NOT AN EDGE MEASURE AND MUST NOT BE RANKED ON.

    It is contaminated by skew, and the contamination has a fixed sign. A
    single-vol lognormal cannot reproduce a skewed market: for a put DEBIT
    spread the market prices the lower-strike short leg relatively richer than
    any flat-vol model will, so the model always values the structure ABOVE the
    market. Measured live, every SPY put debit spread showed a large positive
    "premium" while SPY put IV (14.5%) was in fact ABOVE realised vol (13.2%) -
    a genuine vol premium would have had the opposite sign.

    Separating skew from a true vol premium needs a smile-aware model compared
    against a real-world distribution carrying the same skew. That is out of
    scope here, so this number is reported for transparency and used for
    nothing.
    """
    result = structure.ev()
    return result.variance_premium if result.valid else 0.0


def build_vertical_credit_spreads(
    contracts: list[Contract],
    settings: Settings,
    *,
    kind: str,
    max_candidates: int = 12,
    spot: float | None = None,
    max_loss_cap: float | None = None,
    realised_vols: dict[str, float] | None = None,
) -> list[Structure]:
    """Construct defined-risk vertical CREDIT spreads.

    ``kind`` "C" builds bear call spreads (sell the nearer call, buy the further
    one); "P" builds bull put spreads. Max loss is width minus credit, known
    exactly at entry.
    """
    budget = settings.risk
    forwards = forwards_by_series(contracts)
    usable = [c for c in contracts if c.kind == kind and tradeable(c, settings)]
    if len(usable) < 2:
        return []

    by_series: dict[tuple[str, date], list[Contract]] = {}
    for contract in usable:
        by_series.setdefault((contract.underlying, contract.expiry), []).append(contract)

    candidates: list[Structure] = []
    for (_underlying, expiry), group in by_series.items():
        group.sort(key=lambda c: c.strike)
        # short leg near the target delta: enough premium to be worth selling,
        # far enough OTM to keep a high probability of expiring worthless
        anchor = min(
            group,
            key=lambda c: abs(abs(c.delta) - budget.credit_short_target_delta),
        )
        for long in group:
            if not _is_valid_credit_pair(long, anchor, kind):
                continue
            reference = spot or anchor.strike
            if abs(anchor.strike - long.strike) > reference * budget.max_width_pct_of_spot:
                continue
            structure = _price_credit_spread(
                long, anchor, kind, expiry, spot=spot,
                realised_vol=(realised_vols or {}).get(anchor.underlying, 0.0),
                forward=forwards.get((anchor.underlying, expiry), 0.0))
            if structure is not None:
                candidates.append(structure)

    # Enforce the per-trade loss cap HERE, not downstream. Ranking cannot be
    # relied on to preserve compliant candidates: sorting by EV (absolute or
    # risk-adjusted) favours wide, high-notional spreads, which crowded every
    # affordable candidate off a truncated shortlist. The generator must not
    # emit what the risk engine will certainly refuse.
    if max_loss_cap is not None:
        candidates = [c for c in candidates if c.max_loss <= max_loss_cap]

    candidates.sort(key=lambda s: -quality_score(s))
    return candidates[:max_candidates]


def _price_credit_spread(
    long: Contract, short: Contract, kind: str, expiry: date,
    *, spot: float = 0.0, realised_vol: float = 0.0, forward: float = 0.0,
) -> Structure | None:
    """Price a credit vertical from real quotes. Returns None if not viable."""
    if long.underlying != short.underlying or long.expiry != short.expiry \
            or long.kind != short.kind:
        return None
    width = abs(long.strike - short.strike)
    if width <= 0:
        return None

    # we sell the short at the bid and buy the long at the ask: the honest credit
    credit = short.bid - long.ask
    if credit <= 0:
        return None                       # not a credit spread
    if credit >= width:
        return None                       # implausible: free money

    name = "bear_call_spread" if kind == "C" else "bull_put_spread"
    direction = "bearish" if kind == "C" else "bullish"
    return Structure(
        name=name,
        underlying=long.underlying,
        expiry=expiry,
        qty=1,
        long=long,
        short=short,
        net_debit=-credit,                # NEGATIVE: Alpaca's credit convention
        max_loss=(width - credit) * CONTRACT_MULTIPLIER,
        max_gain=credit * CONTRACT_MULTIPLIER,
        spot=spot or long.strike,
        realised_vol=realised_vol or long.iv,
        forward=forward,
        rationale=(
            f"{direction} {width:.0f}-wide credit vertical, "
            f"collect {credit:.2f}, risk {width - credit:.2f}"
        ),
    )


def probability_of_profit(structure: Structure) -> float:
    result = structure.ev()
    return result.probability_of_profit if result.valid else 0.0


def quality_score(structure: Structure) -> float:
    """Ranking score built ONLY from quantities we can defend.

    Three failed attempts preceded this one, and the pattern in all three was
    the same - ranking on a number that looked like edge and was not:

      1. Reward/risk: always picked the widest spread with the most worthless
         short leg, i.e. a long option wearing a costume.
      2. Delta-weighted EV: delta IS the risk-neutral probability, so measuring
         market prices against it returns ~zero by construction. Any positive
         value was an artifact of a two-point approximation that discarded
         4.6%-10.9% of the probability mass.
      3. Variance premium: dominated by skew, with a fixed sign per structure
         type. See ``variance_premium``.

    So this deliberately makes NO alpha claim. It ranks on execution quality and
    payoff shape, both of which are either observed or robust:

      * execution cost as a fraction of capital at risk - observed from quotes,
        no model, and lower is unambiguously better;
      * probability of profit under realised vol - a model number, but one that
        depends on the payoff geometry rather than on a mispricing claim.

    Selection here is a filter for tradeable structures, not a forecast.
    """
    if structure.max_loss <= 0:
        return 0.0
    friction = execution_cost(structure) / structure.max_loss
    return probability_of_profit(structure) - friction


def risk_adjusted_ev(structure: Structure) -> float:
    """Retained for reporting. Ranking uses ``quality_score``."""
    if structure.max_loss <= 0:
        return 0.0
    return expected_value(structure) / structure.max_loss


def fair_value_ev(structure: Structure) -> float:
    """EV under realised vol if we could transact at the midpoint of both legs."""
    mid_debit = structure.long.mid - structure.short.mid
    result = structure.ev(net_debit=mid_debit)
    return result.ev_under_rv if result.valid else 0.0


def execution_drag(structure: Structure) -> float:
    """Expected value surrendered to the bid-ask spread. Same units as EV."""
    return fair_value_ev(structure) - expected_value(structure)


def _is_valid_credit_pair(long: Contract, short: Contract, kind: str) -> bool:
    """Strike ordering for a CREDIT vertical - the inverse of the debit case.

    Bear call spread: sell the LOWER call, buy the HIGHER one for protection.
    Bull put spread:  sell the HIGHER put, buy the LOWER one for protection.
    """
    if long.symbol == short.symbol:
        return False
    if long.underlying != short.underlying or long.expiry != short.expiry \
            or long.kind != short.kind:
        return False
    if kind == "C":
        return long.strike > short.strike
    return long.strike < short.strike


def _is_valid_pair(long: Contract, short: Contract, kind: str) -> bool:
    if long.symbol == short.symbol:
        return False
    # A vertical is same underlying, same expiry, same kind - different strike.
    if long.underlying != short.underlying:
        return False
    if long.expiry != short.expiry:
        return False
    if long.kind != short.kind:
        return False
    if kind == "C":
        return short.strike > long.strike     # bull call: sell the higher strike
    return short.strike < long.strike         # bear put: sell the lower strike


def _price_debit_spread(
    long: Contract, short: Contract, kind: str, expiry: date,
    *, spot: float = 0.0, realised_vol: float = 0.0, forward: float = 0.0,
) -> Structure | None:
    """Price a debit vertical from real quotes. Returns None if not viable."""
    if long.underlying != short.underlying or long.expiry != short.expiry \
            or long.kind != short.kind:
        return None
    width = abs(long.strike - short.strike)
    if width <= 0:
        return None

    # we buy the long at the ask and sell the short at the bid: the honest cost
    net_debit = long.ask - short.bid
    if net_debit <= 0:
        return None                       # not a debit spread; skip
    if net_debit >= width:
        return None                       # no upside: pay more than we can make

    max_loss = net_debit * CONTRACT_MULTIPLIER
    max_gain = (width - net_debit) * CONTRACT_MULTIPLIER
    name = "bull_call_spread" if kind == "C" else "bear_put_spread"
    direction = "bullish" if kind == "C" else "bearish"

    return Structure(
        name=name,
        underlying=long.underlying,
        expiry=expiry,
        qty=1,
        long=long,
        short=short,
        net_debit=net_debit,
        max_loss=max_loss,
        max_gain=max_gain,
        rationale=(
            f"{direction} {width:.0f}-wide vertical, "
            f"pay {net_debit:.2f} to make at most {width - net_debit:.2f}"
        ),
    )
