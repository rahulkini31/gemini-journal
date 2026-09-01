"""Market data access.

Every gotcha encoded here was verified against the live account on 2026-09-01;
see knowledge-base/10-verification-log.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import DATA_HOST, PAPER_HOST, Settings
from .http import get_json

OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<kind>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class Contract:
    """One option contract with a usable quote and non-null greeks."""

    symbol: str
    underlying: str
    expiry: date
    kind: str            # "C" or "P"
    strike: float
    bid: float
    ask: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        """Quote width as a fraction of mid. The indicative feed is wide -
        a near-ATM SPY call was measured at 5.63/6.83, ~19%."""
        if self.mid <= 0:
            return 1.0
        return (self.ask - self.bid) / self.mid

    def dte(self, today: date | None = None) -> int:
        return (self.expiry - (today or date.today())).days


def parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    """AAPL231201C00195000 -> ("AAPL", 2023-12-01, "C", 195.0)"""
    match = OCC_RE.match(symbol)
    if not match:
        return None
    try:
        expiry = datetime.strptime(match["exp"], "%y%m%d").date()
    except ValueError:
        return None
    return match["root"], expiry, match["kind"], int(match["strike"]) / 1000.0


def market_clock(settings: Settings) -> dict:
    return get_json(f"{PAPER_HOST}/v2/clock", settings.auth_headers)


def underlying_price(settings: Settings, symbol: str) -> float | None:
    """Latest trade price from IEX (the only stock feed on the Basic plan)."""
    data = get_json(
        f"{DATA_HOST}/v2/stocks/{symbol}/snapshot",
        settings.auth_headers,
        {"feed": "iex"},
    )
    for key in ("latestTrade", "dailyBar", "prevDailyBar"):
        node = data.get(key) or {}
        price = node.get("p") or node.get("c")
        if price:
            return float(price)
    return None


def option_chain(
    settings: Settings,
    underlying: str,
    *,
    spot: float,
    strike_band_pct: float = 0.10,
    limit: int = 500,
) -> list[Contract]:
    """Fetch a live option chain, strike- and expiry-bounded.

    Two defences, both from measured failures:

    1. ``expiration_date_gte`` is ALWAYS sent. Without it the endpoint returns
       EXPIRED contracts - we observed Aug 31 contracts returned on Sep 1.
    2. Contracts with null greeks are DROPPED, never defaulted to zero. Expired
       contracts return ``greeks: null``; coercing that to 0.0 would size a
       position off a delta of zero.
    """
    today = date.today()
    contracts: list[Contract] = []
    page_token: str | None = None
    budget = settings.risk

    while True:
        payload = get_json(
            f"{DATA_HOST}/v1beta1/options/snapshots/{underlying}",
            settings.auth_headers,
            {
                "feed": "indicative",
                "strike_price_gte": round(spot * (1 - strike_band_pct), 2),
                "strike_price_lte": round(spot * (1 + strike_band_pct), 2),
                "expiration_date_gte": (today + timedelta(days=budget.min_dte)).isoformat(),
                "expiration_date_lte": (today + timedelta(days=budget.max_dte)).isoformat(),
                "limit": min(limit, 1000),
                "page_token": page_token,
            },
        )
        for symbol, snapshot in (payload.get("snapshots") or {}).items():
            contract = _to_contract(symbol, snapshot)
            if contract is not None:
                contracts.append(contract)
        page_token = payload.get("next_page_token")
        if not page_token:
            break

    return contracts


def _to_contract(symbol: str, snapshot: dict) -> Contract | None:
    """Build a Contract, or None if the snapshot is unusable.

    Returning None rather than a zero-filled object is the whole point: an
    unusable contract must be invisible to the strategy, not silently neutral.
    """
    parsed = parse_occ(symbol)
    if parsed is None:
        return None
    underlying, expiry, kind, strike = parsed

    greeks = snapshot.get("greeks")
    iv = snapshot.get("impliedVolatility")
    if not greeks or iv is None:
        return None  # expired or unpriced - hard reject

    required = ("delta", "gamma", "theta", "vega")
    if any(greeks.get(name) is None for name in required):
        return None

    quote = snapshot.get("latestQuote") or {}
    bid, ask = quote.get("bp"), quote.get("ap")
    if bid is None or ask is None:
        return None
    bid, ask = float(bid), float(ask)
    if ask <= 0 or bid <= 0 or ask < bid:
        return None

    return Contract(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        kind=kind,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=float(greeks["delta"]),
        gamma=float(greeks["gamma"]),
        theta=float(greeks["theta"]),
        vega=float(greeks["vega"]),
        iv=float(iv),
    )


def tradeable(contract: Contract, settings: Settings) -> bool:
    """Liquidity filter. The indicative feed quotes some contracts absurdly wide."""
    budget = settings.risk
    return (
        contract.bid >= budget.min_bid
        and contract.spread_pct <= budget.max_quote_spread_pct
    )
