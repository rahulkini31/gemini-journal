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
    net_debit: float          # per spread, per share (positive = we pay)
    max_loss: float           # dollars, total for qty
    max_gain: float           # dollars, total for qty
    rationale: str = ""

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
        """Conservative limit for a net-debit spread.

        We pay between the natural mid and the full ask. Never assume a mid
        fill - the indicative feed quotes wide (a near-ATM SPY call was
        measured at 5.63/6.83, ~19%), so a mid-priced order often just sits.
        ``aggression`` 0.0 = mid, 1.0 = pay the full spread.
        """
        natural = self.long.ask - self.short.bid   # worst case
        mid = self.long.mid - self.short.mid       # optimistic
        price = mid + (natural - mid) * aggression
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
            "limit_price": self.limit_price(),
            "max_loss": round(self.max_loss, 2),
            "max_gain": round(self.max_gain, 2),
            "reward_risk": round(self.max_gain / self.max_loss, 2) if self.max_loss else 0,
            "expected_value": round(expected_value(self), 2),
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

    # Rank by a crude delta-weighted expected value rather than raw reward/risk.
    # Raw R:R always prefers the widest spread with the most worthless short leg;
    # EV penalises the low probability of ever reaching that payoff.
    candidates.sort(key=lambda s: -expected_value(s))
    return candidates[:max_candidates]


def expected_value_at(structure: Structure, debit: float) -> float:
    """Delta-weighted EV proxy for a given entry cost, in dollars."""
    p_max_gain = abs(structure.short.delta)
    p_max_loss = 1.0 - abs(structure.long.delta)
    max_gain = (structure.width - debit) * CONTRACT_MULTIPLIER * structure.qty
    max_loss = debit * CONTRACT_MULTIPLIER * structure.qty
    return p_max_gain * max_gain - p_max_loss * max_loss


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
