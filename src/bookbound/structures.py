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
    rationale: str = ""

    @property
    def is_credit(self) -> bool:
        return self.net_debit < 0

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
            "expected_value": round(expected_value(self), 2),
            "ev_per_dollar_risked": round(risk_adjusted_ev(self), 4),
            "fair_value_ev": round(fair_value_ev(self), 2),
            "execution_drag": round(execution_drag(self), 2),
            "long_delta": round(self.long.delta, 4),
            "short_delta": round(self.short.delta, 4),
            "long_iv": round(self.long.iv, 4),
            "short_iv": round(self.short.iv, 4),
            "rationale": self.rationale,
        }


def build_vertical_debit_spreads(
    contracts: list[Contract],
    settings: Settings,
    *,
    kind: str,
    target_delta: float = 0.45,
    max_candidates: int = 12,
    spot: float | None = None,
    max_loss_cap: float | None = None,
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
            structure = _price_debit_spread(anchor, short, kind, expiry)
            if structure is not None:
                candidates.append(structure)

    # Enforce the per-trade loss cap HERE, not downstream. Ranking cannot be
    # relied on to preserve compliant candidates: sorting by EV (absolute or
    # risk-adjusted) favours wide, high-notional spreads, which crowded every
    # affordable candidate off a truncated shortlist. The generator must not
    # emit what the risk engine will certainly refuse.
    if max_loss_cap is not None:
        candidates = [c for c in candidates if c.max_loss <= max_loss_cap]

    candidates.sort(key=lambda s: -risk_adjusted_ev(s))
    return candidates[:max_candidates]


def expected_value_at(structure: Structure, net_debit: float) -> float:
    """Delta-weighted EV proxy for a given entry price, in dollars.

    Delta approximates the risk-neutral probability of finishing ITM.

    DEBIT spread (we buy the near strike, sell the far one):
        max gain needs spot beyond the SHORT strike  -> p ~ |short delta|
        max loss needs spot below the LONG strike    -> p ~ 1 - |long delta|

    CREDIT spread (we sell the near strike, buy the far one):
        max gain needs the SHORT to expire worthless -> p ~ 1 - |short delta|
        max loss needs spot beyond the LONG strike   -> p ~ |long delta|
    """
    scale = CONTRACT_MULTIPLIER * structure.qty
    if net_debit < 0:                       # credit
        credit = -net_debit
        max_gain = credit * scale
        max_loss = (structure.width - credit) * scale
        p_max_gain = 1.0 - abs(structure.short.delta)
        p_max_loss = abs(structure.long.delta)
    else:                                   # debit
        max_gain = (structure.width - net_debit) * scale
        max_loss = net_debit * scale
        p_max_gain = abs(structure.short.delta)
        p_max_loss = 1.0 - abs(structure.long.delta)
    return p_max_gain * max_gain - p_max_loss * max_loss


def build_vertical_credit_spreads(
    contracts: list[Contract],
    settings: Settings,
    *,
    kind: str,
    max_candidates: int = 12,
    spot: float | None = None,
    max_loss_cap: float | None = None,
) -> list[Structure]:
    """Construct defined-risk vertical CREDIT spreads.

    ``kind`` "C" builds bear call spreads (sell the nearer call, buy the further
    one); "P" builds bull put spreads. Max loss is width minus credit, known
    exactly at entry.
    """
    budget = settings.risk
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
            structure = _price_credit_spread(long, anchor, kind, expiry)
            if structure is not None:
                candidates.append(structure)

    # Enforce the per-trade loss cap HERE, not downstream. Ranking cannot be
    # relied on to preserve compliant candidates: sorting by EV (absolute or
    # risk-adjusted) favours wide, high-notional spreads, which crowded every
    # affordable candidate off a truncated shortlist. The generator must not
    # emit what the risk engine will certainly refuse.
    if max_loss_cap is not None:
        candidates = [c for c in candidates if c.max_loss <= max_loss_cap]

    candidates.sort(key=lambda s: -risk_adjusted_ev(s))
    return candidates[:max_candidates]


def _price_credit_spread(
    long: Contract, short: Contract, kind: str, expiry: date
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
        rationale=(
            f"{direction} {width:.0f}-wide credit vertical, "
            f"collect {credit:.2f}, risk {width - credit:.2f}"
        ),
    )


def risk_adjusted_ev(structure: Structure) -> float:
    """Expected value per dollar of capital at risk.

    Ranking on ABSOLUTE expected value is a trap. A wide, high-notional spread
    always shows a bigger EV than a narrow one, so sorting by it and truncating
    the shortlist crowded out every candidate small enough to pass the per-trade
    loss cap - the builder produced 24 credit spreads and the risk engine refused
    all 24 for exceeding it.

    Normalising by max loss ranks capital efficiency instead, which is both the
    economically meaningful comparison and the one that keeps admissible
    candidates on the shortlist.
    """
    if structure.max_loss <= 0:
        return 0.0
    return expected_value(structure) / structure.max_loss


def fair_value_ev(structure: Structure) -> float:
    """EV if we could transact at the midpoint of both legs.

    On an efficient market this lands near zero. What matters is not its level
    but the gap between it and the EV at the price we can actually pay - that
    gap is the execution cost the indicative feed imposes on us.
    """
    mid_debit = structure.long.mid - structure.short.mid
    return expected_value_at(structure, mid_debit)


def execution_drag(structure: Structure) -> float:
    """Dollars of expected value surrendered to the bid-ask spread.

    Positive means crossing the spread costs us. This is the number the free
    INDICATIVE feed makes large, and the reason most candidates should be
    refused rather than traded.
    """
    return fair_value_ev(structure) - expected_value(structure)


def expected_value(structure: Structure) -> float:
    """Delta-weighted EV proxy, in dollars.

    Option delta approximates the risk-neutral probability of finishing ITM, so:
      P(reach max gain) ~ |short delta|   (spot beyond the short strike)
      P(total loss)     ~ 1 - |long delta| (spot below the long strike)

    This is a ranking heuristic on top of INDICATIVE-feed greeks, not a pricing
    model. It exists to stop reward/risk from always winning.
    """
    return expected_value_at(structure, structure.net_debit)


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
    long: Contract, short: Contract, kind: str, expiry: date
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
