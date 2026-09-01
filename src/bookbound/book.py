"""Portfolio state aggregated into book-level greeks.

This is the core of Bookbound. Competing systems ask "is this trade safe?".
We ask "what does the book look like after this trade?" - because five
individually-safe bull spreads are collectively a directional bet nobody
approved.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

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

    @classmethod
    def from_contract(cls, contract: Contract, qty: int) -> "Leg":
        return cls(
            symbol=contract.symbol,
            qty=qty,
            delta=contract.delta,
            gamma=contract.gamma,
            theta=contract.theta,
            vega=contract.vega,
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
        scale = leg.qty * CONTRACT_MULTIPLIER
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

    @property
    def greeks(self) -> Greeks:
        return greeks_of_legs(self.legs)

    @property
    def day_pnl_pct(self) -> float:
        if self.last_equity <= 0:
            return 0.0
        return (self.equity - self.last_equity) / self.last_equity

    def positions_by_underlying(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for leg in self.legs:
            parsed = parse_occ(leg.symbol)
            if parsed:
                counts[parsed[0]] += 1
        return dict(counts)

    def with_added(self, legs: list[Leg]) -> "Book":
        """Hypothetical book after adding legs. Used to gate on the *result*."""
        return replace(self, legs=[*self.legs, *legs])


def load_book(settings: Settings, quotes: dict[str, Contract] | None = None) -> Book:
    """Fetch account + positions and aggregate into a Book.

    Alpaca's position payload does not carry greeks, so ``quotes`` supplies them
    from the current chain snapshot. A held option we have no fresh greeks for is
    NOT silently treated as delta-zero - it is reported by ``missing_greeks`` so
    the risk engine can refuse to trade against an unknown book.
    """
    account = get_json(f"{PAPER_HOST}/v2/account", settings.auth_headers)
    positions = get_json(f"{PAPER_HOST}/v2/positions", settings.auth_headers)
    quotes = quotes or {}

    legs: list[Leg] = []
    for position in positions:
        if position.get("asset_class") != "us_option":
            continue
        symbol = position["symbol"]
        qty = int(float(position["qty"]))
        if position.get("side") == "short" and qty > 0:
            qty = -qty
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
    )


def missing_greeks(book: Book, quotes: dict[str, Contract]) -> list[str]:
    """Held option symbols we could not price. A non-empty result means the
    book's true greeks are unknown and the risk engine must fail closed."""
    return [
        position["symbol"]
        for position in book.raw_positions
        if position.get("asset_class") == "us_option"
        and position["symbol"] not in quotes
    ]
