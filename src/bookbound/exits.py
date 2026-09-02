"""Exit management.

Entries without exits is not a strategy, it is a collection of lottery tickets.
Every rule here is deterministic - no model decides when to close a position.

For a debit vertical the exit ladder is:
  * profit target - bank a fraction of max gain rather than holding for the last
    few cents, which are the hardest to realise on a wide indicative quote
  * stop loss    - cut before max loss, so the realised loss is smaller than the
    theoretical one the risk engine budgeted for
  * time stop    - close well before expiry. Paper syncs option non-trade
    activities the NEXT DAY, so letting anything expire inside the judging
    window produces effects that land after judging, or not at all.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_CEILING
import math

from .config import Settings
from .market import Contract, cross_leg_quote_skew_seconds, parse_occ


@dataclass(frozen=True)
class ExitOrder:
    """An instruction to close one open option position."""

    symbol: str
    qty: int                 # positive; direction comes from `side`
    side: str                # "buy" closes a short, "sell" closes a long
    position_intent: str
    reason: str
    detail: str


@dataclass(frozen=True)
class ExitLeg:
    """One leg inside a structure-level close instruction.

    ``qty`` is the absolute quantity held.  Multi-leg execution converts the
    quantities to a relatively-prime ratio without changing their total size.
    """

    symbol: str
    qty: int
    side: str
    position_intent: str


@dataclass(frozen=True)
class ExitPlan:
    """A close instruction that must be submitted as one broker order.

    A plan may contain one leg only when reconciliation has found an already
    naked position.  A healthy vertical always has two legs in the same plan.
    """

    underlying: str
    expiry: date
    kind: str
    legs: tuple[ExitLeg, ...]
    reason: str
    detail: str
    cost_basis: float = 0.0
    unrealized_pl: float = 0.0
    # Alpaca MLEG convention: positive is a debit paid, negative is a credit
    # received.  A single-leg payload takes the absolute value because its side
    # carries the cash-flow direction.
    limit_price: float | None = None
    limit_source: str | None = None
    limit_as_of: datetime | None = None

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(leg.symbol for leg in self.legs)


QUOTE_NATURAL_LIMIT_SOURCE = "executable_side_natural_quote"


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _marketable_cent(value: Decimal) -> Decimal:
    """Round a signed net natural price in the marketable direction.

    ``ROUND_CEILING`` is deliberately asymmetric: a debit rounds up while a
    credit rounds toward zero.  Both concede less than one cent rather than
    accidentally posting a price just outside the executable quote.  Alpaca
    rejects a zero MLEG limit, so an exactly flat natural is capped at a one
    cent debit instead of becoming an unbounded order.
    """
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return Decimal("0.01") if rounded == 0 else rounded


def price_exit_plan(
    plan: ExitPlan,
    quotes: dict[str, Contract],
    settings: Settings,
    *,
    as_of: datetime,
) -> ExitPlan:
    """Attach a fresh, executable-side natural limit to an exit plan.

    Pricing is all-or-nothing.  Every leg must have an exact quote, a fresh
    timezone-aware timestamp, displayed size on the side this close consumes,
    and a timestamp synchronized with the other legs.  This is intentionally
    separate from candidate ``tradeable`` checks: urgent exits may be inside
    the entry DTE range or have a zero non-executable-side bid.
    """
    reference = _aware_utc(as_of, field="as_of")
    if not plan.legs:
        raise ValueError("cannot price an exit plan without legs")
    if len({leg.symbol for leg in plan.legs}) != len(plan.legs):
        raise ValueError("cannot price duplicate exit symbols")

    max_age = float(getattr(
        settings.risk,
        "max_exit_quote_age_seconds",
        settings.risk.max_quote_age_seconds,
    ))
    max_future_skew = float(getattr(
        settings.risk, "max_future_quote_skew_seconds", 2.0,
    ))
    min_size = int(getattr(settings.risk, "min_quote_size", 1))
    selected: list[Contract] = []

    for leg in plan.legs:
        quote = quotes.get(leg.symbol)
        if quote is None:
            raise ValueError(f"missing exact exit quote for {leg.symbol}")
        if quote.symbol != leg.symbol:
            raise ValueError(f"mismatched exit quote for {leg.symbol}")
        parsed = parse_occ(leg.symbol)
        if parsed is None:
            raise ValueError(f"invalid OCC exit symbol {leg.symbol}")
        underlying, expiry, kind, _ = parsed
        if (underlying, expiry, kind) != (
            plan.underlying, plan.expiry, plan.kind,
        ):
            raise ValueError(f"{leg.symbol} is outside the exit-plan series")

        numeric = (quote.bid, quote.ask)
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError(f"non-finite exit quote for {leg.symbol}")
        if quote.bid < 0 or quote.ask <= 0 or quote.ask < quote.bid:
            raise ValueError(f"crossed or invalid exit quote for {leg.symbol}")
        if quote.quote_timestamp is None:
            raise ValueError(f"missing exit quote timestamp for {leg.symbol}")
        observed = _aware_utc(
            quote.quote_timestamp, field=f"quote timestamp for {leg.symbol}",
        )
        age = (reference - observed).total_seconds()
        if age > max_age:
            raise ValueError(f"stale exit quote for {leg.symbol}: {age:.1f}s")
        if age < -max_future_skew:
            raise ValueError(f"future-dated exit quote for {leg.symbol}: {age:.1f}s")

        if leg.side == "buy":
            size = quote.ask_size
            side_name = "ask"
        elif leg.side == "sell":
            size = quote.bid_size
            side_name = "bid"
            if quote.bid <= 0:
                raise ValueError(f"no executable bid for {leg.symbol}")
        else:
            raise ValueError(f"invalid exit side for {leg.symbol}: {leg.side!r}")
        if size is None or size < min_size:
            raise ValueError(
                f"missing executable {side_name} size for {leg.symbol}"
            )
        selected.append(quote)

    skew = cross_leg_quote_skew_seconds(selected)
    max_leg_skew = float(getattr(
        settings.risk, "max_leg_quote_skew_seconds", 30.0,
    ))
    if skew is None or skew > max_leg_skew:
        raise ValueError(
            "exit leg quotes are not synchronous: "
            f"{skew!r}s vs {max_leg_skew:.1f}s maximum"
        )

    base_qty = math.gcd(*(leg.qty for leg in plan.legs))
    if base_qty <= 0:
        raise ValueError("exit leg quantities must be positive")
    natural = Decimal("0")
    for leg in plan.legs:
        quote = quotes[leg.symbol]
        ratio = leg.qty // base_qty
        if ratio <= 0 or leg.qty % base_qty:
            raise ValueError("exit leg quantities cannot be normalized")
        price = quote.ask if leg.side == "buy" else quote.bid
        signed = Decimal(str(price)) * ratio
        natural += signed if leg.side == "buy" else -signed

    bounded = _marketable_cent(natural)
    return replace(
        plan,
        limit_price=float(bounded),
        limit_source=QUOTE_NATURAL_LIMIT_SOURCE,
        limit_as_of=reference,
    )


@dataclass(frozen=True)
class PositionLeg:
    """Normalized broker position used by lifecycle planning.

    Alpaca position payloads have appeared with both an absolute ``qty`` plus a
    ``side`` and a signed ``qty``.  Normalizing once prevents different safety
    layers from disagreeing about which side is short.
    """

    symbol: str
    signed_qty: int
    signed_cost_basis: float
    unrealized_pl: float

    @property
    def qty(self) -> int:
        return abs(self.signed_qty)

    @property
    def is_short(self) -> bool:
        return self.signed_qty < 0

    def close_leg(self, *, qty: int | None = None) -> ExitLeg:
        close_qty = self.qty if qty is None else qty
        return ExitLeg(
            symbol=self.symbol,
            qty=close_qty,
            side="buy" if self.is_short else "sell",
            position_intent="buy_to_close" if self.is_short else "sell_to_close",
        )


@dataclass(frozen=True)
class PositionGroup:
    """Positions sharing the only dimensions that can form a vertical."""

    underlying: str
    expiry: date
    kind: str
    legs: tuple[PositionLeg, ...]

    @property
    def long_qty(self) -> int:
        return sum(leg.signed_qty for leg in self.legs if leg.signed_qty > 0)

    @property
    def short_qty(self) -> int:
        return -sum(leg.signed_qty for leg in self.legs if leg.signed_qty < 0)

    @property
    def is_balanced_vertical(self) -> bool:
        """Whether holdings identify one unambiguous 1:1 vertical.

        Positions alone do not preserve original order IDs.  More than two
        distinct strikes could represent several overlapping spreads, so normal
        exit management fails closed and leaves those groups to reconciliation.
        """
        return (
            len(self.legs) == 2
            and self.long_qty > 0
            and self.long_qty == self.short_qty
            and sum(1 for leg in self.legs if not leg.is_short) == 1
            and sum(1 for leg in self.legs if leg.is_short) == 1
        )

    @property
    def cost_basis(self) -> float:
        """Absolute net entry cash flow for the complete spread."""
        return abs(sum(leg.signed_cost_basis for leg in self.legs))

    @property
    def unrealized_pl(self) -> float:
        return sum(leg.unrealized_pl for leg in self.legs)

    def close_plan(self, *, reason: str, detail: str) -> ExitPlan:
        return ExitPlan(
            underlying=self.underlying,
            expiry=self.expiry,
            kind=self.kind,
            legs=tuple(leg.close_leg() for leg in self.legs),
            reason=reason,
            detail=detail,
            cost_basis=self.cost_basis,
            unrealized_pl=self.unrealized_pl,
        )


def _signed_position_qty(position: dict) -> int:
    qty = int(float(position.get("qty") or 0))
    side = str(position.get("side") or "").lower()
    if side == "short":
        return -abs(qty)
    if side == "long":
        return abs(qty)
    return qty


def _normalized_position(position: dict) -> tuple[tuple[str, date, str], PositionLeg] | None:
    if position.get("asset_class") != "us_option":
        return None
    symbol = position.get("symbol")
    parsed = parse_occ(symbol) if isinstance(symbol, str) else None
    if parsed is None:
        return None
    underlying, expiry, kind, _ = parsed
    signed_qty = _signed_position_qty(position)
    if signed_qty == 0:
        return None

    raw_basis = abs(float(position.get("cost_basis") or 0.0))
    signed_basis = -raw_basis if signed_qty < 0 else raw_basis
    leg = PositionLeg(
        symbol=symbol,
        signed_qty=signed_qty,
        signed_cost_basis=signed_basis,
        unrealized_pl=float(position.get("unrealized_pl") or 0.0),
    )
    return (underlying, expiry, kind), leg


def group_option_positions(positions: list[dict]) -> list[PositionGroup]:
    """Group valid non-zero option positions by underlying, expiry and kind.

    The output order and leg order are deterministic so an audit record can be
    compared byte-for-byte across replays.
    """
    grouped: dict[tuple[str, date, str], list[PositionLeg]] = defaultdict(list)
    for position in positions:
        normalized = _normalized_position(position)
        if normalized is not None:
            key, leg = normalized
            grouped[key].append(leg)

    return [
        PositionGroup(
            underlying=underlying,
            expiry=expiry,
            kind=kind,
            legs=tuple(sorted(grouped[(underlying, expiry, kind)], key=lambda leg: leg.symbol)),
        )
        for underlying, expiry, kind in sorted(grouped)
    ]


def evaluate_structure_exits(
    positions: list[dict],
    settings: Settings,
    *,
    today: date | None = None,
) -> list[ExitPlan]:
    """Evaluate exit rules at vertical level and return atomic close plans.

    Only an unambiguous balanced two-leg vertical is eligible.  Broken ratios
    belong to reconciliation: applying a normal P&L exit to one of their legs
    can increase undefined risk.
    """
    today = today or date.today()
    plans: list[ExitPlan] = []
    for group in group_option_positions(positions):
        if not group.is_balanced_vertical:
            continue
        reason, detail = _structure_exit_reason(group, today, settings)
        if reason is not None:
            plans.append(group.close_plan(reason=reason, detail=detail))
    return plans


# The shorter name reads naturally at call sites and keeps the safety contract
# explicit.  Both names are public so integration code need not guess.
evaluate_spread_exits = evaluate_structure_exits


def _structure_exit_reason(
    group: PositionGroup,
    today: date,
    settings: Settings,
) -> tuple[str | None, str]:
    budget = settings.risk
    dte = (group.expiry - today).days
    if dte <= budget.exit_time_stop_dte:
        return "time_stop", (
            f"{dte} DTE at or below the {budget.exit_time_stop_dte} DTE floor"
        )

    if group.cost_basis <= 0:
        return None, ""
    pnl_pct = group.unrealized_pl / group.cost_basis
    if pnl_pct >= budget.exit_profit_target_pct:
        return "profit_target", (
            f"spread {pnl_pct:+.1%} vs target +{budget.exit_profit_target_pct:.0%} "
            f"({group.unrealized_pl:+.2f} on {group.cost_basis:.2f} net basis)"
        )
    if pnl_pct <= -budget.exit_stop_loss_pct:
        return "stop_loss", (
            f"spread {pnl_pct:+.1%} vs stop -{budget.exit_stop_loss_pct:.0%} "
            f"({group.unrealized_pl:+.2f} on {group.cost_basis:.2f} net basis)"
        )
    return None, ""


def flatten_structure_plans(positions: list[dict], reason: str) -> list[ExitPlan]:
    """Plan one close operation per option series for kill/deadline handling.

    Two-leg groups can be submitted atomically.  Malformed groups remain grouped
    in a single plan so the execution layer can either submit them atomically or
    fail closed; this function never silently decomposes a spread into leg
    orders.
    """
    return [
        group.close_plan(reason="flatten", detail=reason)
        for group in group_option_positions(positions)
    ]


def evaluate_exits(
    positions: list[dict],
    quotes: dict[str, Contract],
    settings: Settings,
    *,
    today: date | None = None,
) -> list[ExitOrder]:
    """Return close instructions for every position that has hit an exit rule."""
    today = today or date.today()
    budget = settings.risk
    orders: list[ExitOrder] = []

    for position in positions:
        if position.get("asset_class") != "us_option":
            continue

        symbol = position["symbol"]
        parsed = parse_occ(symbol)
        if not parsed:
            continue
        _, expiry, _, _ = parsed

        qty = abs(int(float(position["qty"])))
        if qty == 0:
            continue
        is_short = position.get("side") == "short" or float(position["qty"]) < 0

        reason, detail = _exit_reason(position, expiry, today, budget, is_short)
        if reason is None:
            continue

        orders.append(
            ExitOrder(
                symbol=symbol,
                qty=qty,
                side="buy" if is_short else "sell",
                position_intent="buy_to_close" if is_short else "sell_to_close",
                reason=reason,
                detail=detail,
            )
        )
    return orders


def _exit_reason(position, expiry, today, budget, is_short):
    """Deterministic exit ladder. Time stop is checked first - it is the only
    rule that is about the calendar rather than the P&L, and it is absolute."""
    dte = (expiry - today).days
    if dte <= budget.exit_time_stop_dte:
        return "time_stop", f"{dte} DTE at or below the {budget.exit_time_stop_dte} DTE floor"

    cost_basis = abs(float(position.get("cost_basis") or 0.0))
    unrealised = float(position.get("unrealized_pl") or 0.0)
    if cost_basis <= 0:
        return None, ""

    pnl_pct = unrealised / cost_basis
    if pnl_pct >= budget.exit_profit_target_pct:
        return "profit_target", (
            f"+{pnl_pct:.1%} vs target +{budget.exit_profit_target_pct:.0%}"
        )
    if pnl_pct <= -budget.exit_stop_loss_pct:
        return "stop_loss", (
            f"{pnl_pct:.1%} vs stop -{budget.exit_stop_loss_pct:.0%}"
        )
    return None, ""


def deadline_flatten(positions: list[dict], reason: str) -> list[ExitOrder]:
    """Close every option position unconditionally. Used by the kill switch and
    by the pre-deadline flatten."""
    orders: list[ExitOrder] = []
    for position in positions:
        if position.get("asset_class") != "us_option":
            continue
        qty = abs(int(float(position["qty"])))
        if qty == 0:
            continue
        is_short = position.get("side") == "short" or float(position["qty"]) < 0
        orders.append(ExitOrder(
            symbol=position["symbol"], qty=qty,
            side="buy" if is_short else "sell",
            position_intent="buy_to_close" if is_short else "sell_to_close",
            reason="flatten", detail=reason,
        ))
    return orders
