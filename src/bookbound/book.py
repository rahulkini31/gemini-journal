"""Portfolio state aggregated into book-level greeks.

This is the core of Bookbound. Competing systems ask "is this trade safe?".
We ask "what does the book look like after this trade?" - because five
individually-safe bull spreads are collectively a directional bet nobody
approved.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace

from .config import PAPER_HOST, Settings
from .http import get_json
from .market import Contract, parse_occ

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class Leg:
    """One leg of a structure. ``qty`` is signed: positive long, negative short."""

    symbol: str
    qty: int
    delta: float
    gamma: float
    theta: float
    vega: float
    multiplier: int = CONTRACT_MULTIPLIER
    underlying: str | None = None

    @classmethod
    def from_contract(cls, contract: Contract, qty: int) -> "Leg":
        return cls(
            symbol=contract.symbol,
            qty=qty,
            delta=contract.delta,
            gamma=contract.gamma,
            theta=contract.theta,
            vega=contract.vega,
            multiplier=CONTRACT_MULTIPLIER,
            underlying=contract.underlying,
        )


@dataclass(frozen=True)
class Greeks:
    """Book-level greeks, in position units (per-share greek x qty x 100)."""

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0

    def __add__(self, other: "Greeks") -> "Greeks":
        return Greeks(
            self.delta + other.delta,
            self.gamma + other.gamma,
            self.theta + other.theta,
            self.vega + other.vega,
        )

    def scaled(self, factor: float) -> "Greeks":
        return Greeks(
            self.delta * factor,
            self.gamma * factor,
            self.theta * factor,
            self.vega * factor,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "delta": round(self.delta, 2),
            "gamma": round(self.gamma, 3),
            "theta": round(self.theta, 2),
            "vega": round(self.vega, 2),
        }


def greeks_of_legs(legs: list[Leg]) -> Greeks:
    """Aggregate signed legs into book greeks.

    Sign convention: a short leg carries negative qty, which flips every greek.
    Short a 0.30-delta call => -30 delta per contract, positive theta.
    """
    total = Greeks()
    for leg in legs:
        scale = leg.qty * leg.multiplier
        total = total + Greeks(
            leg.delta * scale,
            leg.gamma * scale,
            leg.theta * scale,
            leg.vega * scale,
        )
    return total


@dataclass
class Book:
    """The live portfolio."""

    equity: float
    last_equity: float
    cash: float
    buying_power: float
    options_buying_power: float
    legs: list[Leg]
    raw_positions: list[dict]
    raw_orders: list[dict] = field(default_factory=list)
    pending_legs: list[Leg] = field(default_factory=list)
    spots: dict[str, float] = field(default_factory=dict)

    @property
    def greeks(self) -> Greeks:
        return greeks_of_legs(self.legs)

    @property
    def committed_greeks(self) -> Greeks:
        """Greeks held now plus opening orders that may still fill."""
        return greeks_of_legs([*self.legs, *self.pending_legs])

    @property
    def pending_open_risk(self) -> float:
        return sum(pending_order_max_loss(order) for order in self.raw_orders)

    @property
    def day_pnl_pct(self) -> float:
        if self.last_equity <= 0:
            return 0.0
        return (self.equity - self.last_equity) / self.last_equity

    def positions_by_underlying(self) -> dict[str, int]:
        """Open contract units, rather than distinct symbols.

        Brokers merge repeated fills in the same symbol into one position.  A
        symbol count therefore lets quantity evade a concentration limit.
        """
        counts: dict[str, int] = defaultdict(int)
        for leg in self.legs:
            parsed = parse_occ(leg.symbol)
            if parsed:
                counts[parsed[0]] += abs(leg.qty)
        return dict(counts)

    def with_added(self, legs: list[Leg]) -> "Book":
        """Hypothetical book after adding legs. Used to gate on the *result*."""
        return replace(self, legs=[*self.legs, *legs])


def _signed_position_qty(position: dict) -> int:
    qty = int(float(position.get("qty") or 0))
    if position.get("side") == "short" and qty > 0:
        qty = -qty
    return qty


def _entry_price(position: dict, signed_qty: int) -> float:
    raw = position.get("avg_entry_price")
    if raw not in (None, ""):
        return abs(float(raw))
    basis = abs(float(position.get("cost_basis") or 0.0))
    units = abs(signed_qty) * CONTRACT_MULTIPLIER
    return basis / units if units else 0.0


def _group_max_loss(
    legs: list[tuple[str, float, str, int]], entry_cash: float
) -> float:
    """Worst expiry P&L for one underlying/expiry/kind group.

    ``legs`` contains (kind, strike, symbol, signed_contract_qty).  Linear
    option payoffs only change slope at strikes, so evaluating zero and every
    strike is exact.  A net short-call slope is explicitly unbounded.
    """
    if not legs:
        return 0.0
    kinds = {kind for kind, _, _, _ in legs}
    if len(kinds) != 1:
        return math.inf
    kind = next(iter(kinds))
    strikes = sorted({strike for _, strike, _, _ in legs})
    if kind == "C" and sum(qty for _, _, _, qty in legs) < 0:
        return math.inf
    probes = [0.0, *strikes]
    if kind == "C":
        probes.append((strikes[-1] if strikes else 1.0) * 2.0)
    else:
        probes.append((strikes[-1] if strikes else 1.0) * 2.0)
    pnls = []
    for spot in probes:
        payoff = 0.0
        for leg_kind, strike, _symbol, qty in legs:
            intrinsic = (
                max(spot - strike, 0.0)
                if leg_kind == "C"
                else max(strike - spot, 0.0)
            )
            payoff += qty * intrinsic * CONTRACT_MULTIPLIER
        pnls.append(entry_cash + payoff)
    return max(0.0, -min(pnls))


def option_portfolio_max_loss(positions: list[dict]) -> float:
    """Reconstruct defined-risk expiry loss from complete position payoffs."""
    groups: dict[tuple[str, object, str], list[tuple[dict, tuple]]] = defaultdict(list)
    for position in positions:
        if position.get("asset_class") != "us_option":
            continue
        parsed = parse_occ(str(position.get("symbol") or ""))
        if parsed is None:
            return math.inf
        underlying, expiry, kind, _strike = parsed
        groups[(underlying, expiry, kind)].append((position, parsed))

    total = 0.0
    for rows in groups.values():
        payoff_legs: list[tuple[str, float, str, int]] = []
        entry_cash = 0.0
        for position, parsed in rows:
            _underlying, _expiry, kind, strike = parsed
            qty = _signed_position_qty(position)
            entry_cash -= qty * _entry_price(position, qty) * CONTRACT_MULTIPLIER
            payoff_legs.append((kind, strike, position["symbol"], qty))
        risk = _group_max_loss(payoff_legs, entry_cash)
        if math.isinf(risk):
            return risk
        total += risk
    return total


def _current_position_value(position: dict, signed_qty: int) -> float | None:
    """Signed current option value represented in account equity."""
    raw_market_value = position.get("market_value")
    try:
        if raw_market_value not in (None, ""):
            value = abs(float(raw_market_value))
        elif position.get("current_price") not in (None, ""):
            value = (
                abs(float(position["current_price"]))
                * abs(signed_qty)
                * CONTRACT_MULTIPLIER
            )
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return value if signed_qty > 0 else -value


def option_portfolio_remaining_loss(positions: list[dict]) -> float:
    """Worst expiry loss from *current equity*, using current position marks.

    Inception max loss remains useful attribution, but it is not today's risk:
    unrealized gains are already inside account equity and can be lost again.
    Missing current marks therefore make remaining risk unknown, not zero.
    """
    groups: dict[tuple[str, object, str], list[tuple[dict, tuple]]] = defaultdict(list)
    for position in positions:
        if position.get("asset_class") != "us_option":
            continue
        parsed = parse_occ(str(position.get("symbol") or ""))
        if parsed is None:
            return math.inf
        underlying, expiry, kind, _strike = parsed
        groups[(underlying, expiry, kind)].append((position, parsed))

    total = 0.0
    for rows in groups.values():
        payoff_legs: list[tuple[str, float, str, int]] = []
        current_value = 0.0
        for position, parsed in rows:
            _underlying, _expiry, kind, strike = parsed
            qty = _signed_position_qty(position)
            mark = _current_position_value(position, qty)
            if mark is None:
                return math.inf
            current_value += mark
            payoff_legs.append((kind, strike, position["symbol"], qty))
        risk = _group_max_loss(payoff_legs, -current_value)
        if math.isinf(risk):
            return risk
        total += risk
    return total


def pending_order_max_loss(order: dict) -> float:
    """Maximum loss reserved by one working opening multi-leg order."""
    if str(order.get("status", "")).lower() in {
        "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"
    }:
        return 0.0
    parent_qty = max(
        0,
        abs(int(float(order.get("qty") or 0)))
        - abs(int(float(order.get("filled_qty") or 0))),
    )
    if parent_qty <= 0:
        return 0.0
    payoff_legs: list[tuple[str, float, str, int]] = []
    series: set[tuple[str, object, str]] = set()
    nodes = list(order.get("legs") or [])
    if not nodes and order.get("symbol"):
        nodes = [order]
    for node in nodes:
        intent = str(node.get("position_intent") or "")
        if intent and not intent.endswith("_to_open"):
            continue
        parsed = parse_occ(str(node.get("symbol") or ""))
        if parsed is None:
            return math.inf
        underlying, expiry, kind, strike = parsed
        series.add((underlying, expiry, kind))
        ratio = abs(int(float(node.get("ratio_qty") or 1)))
        signed = ratio * parent_qty * (1 if node.get("side") == "buy" else -1)
        payoff_legs.append((kind, strike, node["symbol"], signed))
    if not payoff_legs:
        return 0.0
    if len(series) != 1:
        return math.inf
    if str(order.get("type") or "limit").lower() != "limit":
        return math.inf
    if order.get("limit_price") in (None, ""):
        return math.inf
    # Alpaca's parent limit uses positive debit / negative credit, per one ratio.
    entry_cash = -float(order.get("limit_price") or 0.0) * parent_qty * CONTRACT_MULTIPLIER
    return _group_max_loss(payoff_legs, entry_cash)


def dollar_delta_by_underlying(
    legs: list[Leg], spots: dict[str, float], *, move_pct: float = 0.01
) -> dict[str, float]:
    """First-order dollar P&L for a percentage move, bucketed by underlying."""
    out: dict[str, float] = defaultdict(float)
    for leg in legs:
        parsed = parse_occ(leg.symbol)
        underlying = leg.underlying or (parsed[0] if parsed else leg.symbol)
        spot = float(spots.get(underlying) or 0.0)
        out[underlying] += leg.delta * leg.qty * leg.multiplier * spot * move_pct
    return dict(out)


def greek_stress_loss(
    legs: list[Leg],
    spots: dict[str, float],
    *,
    spot_shocks: tuple[float, ...] = (-0.03, -0.015, 0.015, 0.03),
    vol_shocks_points: tuple[float, ...] = (-5.0, 5.0),
    days: int = 1,
) -> float:
    """Worst local Greek P&L across deterministic correlated scenarios.

    This is not a replacement for a full repricing engine.  It is a fail-safe
    against cross-underlying netting: all underlyings receive the same percentage
    shock, while their own spot scales delta and gamma economically.
    """
    buckets: dict[str, Greeks] = defaultdict(Greeks)
    for leg in legs:
        parsed = parse_occ(leg.symbol)
        underlying = leg.underlying or (parsed[0] if parsed else leg.symbol)
        buckets[underlying] = buckets[underlying] + greeks_of_legs([leg])

    # Sum each underlying's independently adverse local scenario.  A factor-only
    # shock lets equal dollar deltas in correlated ETFs cancel perfectly even
    # though their basis can widen; this conservative envelope prevents that.
    total_loss = 0.0
    for underlying, greeks in buckets.items():
        spot = float(spots.get(underlying) or 0.0)
        worst_bucket_pnl = 0.0
        for spot_shock in (*spot_shocks, 0.0):
            for vol_shock in (*vol_shocks_points, 0.0):
                d_spot = spot * spot_shock
                pnl = (
                    greeks.delta * d_spot
                    + 0.5 * greeks.gamma * d_spot * d_spot
                    + greeks.vega * vol_shock
                    + greeks.theta * days
                )
                worst_bucket_pnl = min(worst_bucket_pnl, pnl)
        total_loss += max(0.0, -worst_bucket_pnl)
    return total_loss


def _pending_order_legs(orders: list[dict], quotes: dict[str, Contract]) -> list[Leg]:
    pending: list[Leg] = []
    for order in orders:
        if pending_order_max_loss(order) <= 0:
            continue
        parent_qty = max(
            0,
            abs(int(float(order.get("qty") or 0)))
            - abs(int(float(order.get("filled_qty") or 0))),
        )
        nodes = list(order.get("legs") or [])
        if not nodes and order.get("symbol"):
            nodes = [order]
        for node in nodes:
            intent = str(node.get("position_intent") or "")
            if intent and not intent.endswith("_to_open"):
                continue
            contract = quotes.get(str(node.get("symbol") or ""))
            if contract is None:
                continue
            ratio = abs(int(float(node.get("ratio_qty") or 1)))
            qty = parent_qty * ratio * (1 if node.get("side") == "buy" else -1)
            pending.append(Leg.from_contract(contract, qty))
    return pending


def load_book(
    settings: Settings,
    quotes: dict[str, Contract] | None = None,
    *,
    spots: dict[str, float] | None = None,
) -> Book:
    """Fetch account + positions and aggregate into a Book.

    Alpaca's position payload does not carry greeks, so ``quotes`` supplies them
    from the current chain snapshot. A held option we have no fresh greeks for is
    NOT silently treated as delta-zero - it is reported by ``missing_greeks`` so
    the risk engine can refuse to trade against an unknown book.
    """
    account = get_json(f"{PAPER_HOST}/v2/account", settings.auth_headers)
    positions = get_json(f"{PAPER_HOST}/v2/positions", settings.auth_headers)
    orders = get_json(
        f"{PAPER_HOST}/v2/orders", settings.auth_headers,
        {"status": "open", "nested": "true", "limit": 500},
    )
    if not isinstance(orders, list):
        orders = []
    quotes = quotes or {}

    legs: list[Leg] = []
    for position in positions:
        if position.get("asset_class") == "us_equity":
            qty = _signed_position_qty(position)
            legs.append(Leg(
                symbol=str(position.get("symbol") or ""), qty=qty,
                delta=1.0, gamma=0.0, theta=0.0, vega=0.0, multiplier=1,
                underlying=str(position.get("symbol") or ""),
            ))
            continue
        if position.get("asset_class") != "us_option":
            continue
        symbol = position["symbol"]
        qty = _signed_position_qty(position)
        contract = quotes.get(symbol)
        if contract is None:
            continue
        legs.append(Leg.from_contract(contract, qty))

    return Book(
        equity=float(account["equity"]),
        last_equity=float(account.get("last_equity") or account["equity"]),
        cash=float(account["cash"]),
        buying_power=float(account["buying_power"]),
        options_buying_power=float(account.get("options_buying_power") or 0.0),
        legs=legs,
        raw_positions=positions,
        raw_orders=orders,
        pending_legs=_pending_order_legs(orders, quotes),
        spots=dict(spots or {}),
    )


def missing_greeks(book: Book, quotes: dict[str, Contract]) -> list[str]:
    """Held option symbols we could not price. A non-empty result means the
    book's true greeks are unknown and the risk engine must fail closed."""
    missing = {
        position["symbol"]
        for position in book.raw_positions
        if position.get("asset_class") == "us_option"
        and position["symbol"] not in quotes
    }
    terminal = {
        "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"
    }
    for order in book.raw_orders:
        if str(order.get("status") or "").lower() in terminal:
            continue
        nodes = list(order.get("legs") or [])
        if not nodes and order.get("symbol"):
            nodes = [order]
        for node in nodes:
            intent = str(node.get("position_intent") or "")
            if intent and not intent.endswith("_to_open"):
                continue
            symbol = str(node.get("symbol") or "")
            if parse_occ(symbol) and symbol not in quotes:
                missing.add(symbol)
    return sorted(missing)
