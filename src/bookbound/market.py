"""Market data access.

Every gotcha encoded here was verified against the live account on 2026-09-01;
see knowledge-base/10-verification-log.md.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

from .config import DATA_HOST, PAPER_HOST, Settings
from .context import exchange_date
from .http import get_json

OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<kind>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class UnderlyingSnapshot:
    """Timestamped underlier mark used to locate and value option structures."""

    symbol: str
    price: float
    timestamp: datetime | None
    source: str


@dataclass(frozen=True)
class OptionDeliverable:
    """One deliverable from Alpaca's option-contract master."""

    type: str
    symbol: str
    amount: float
    allocation_percentage: float
    settlement_type: str | None = None
    settlement_method: str | None = None
    delayed_settlement: bool | None = None


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
    quote_timestamp: datetime | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    bid_exchange: str | None = None
    ask_exchange: str | None = None
    quote_condition: str | tuple[str, ...] | None = None
    # Contract-master fields are intentionally nullable.  The option snapshot
    # endpoint does not contain deliverable metadata, so an unmerged snapshot
    # must remain distinguishable from a verified standard 100-share contract.
    multiplier: int | None = None
    contract_size: int | None = None
    contract_status: str | None = None
    contract_tradable: bool | None = None
    contract_style: str | None = None
    root_symbol: str | None = None
    deliverables: tuple[OptionDeliverable, ...] | None = None
    metadata_verified: bool = False

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

    @property
    def bid_venue(self) -> str | None:
        """Human-readable alias for Alpaca's bid-exchange field."""
        return self.bid_exchange

    @property
    def ask_venue(self) -> str | None:
        """Human-readable alias for Alpaca's ask-exchange field."""
        return self.ask_exchange

    @property
    def quote_conditions(self) -> tuple[str, ...]:
        """Expose quote conditions uniformly even when the feed sends one."""
        if self.quote_condition is None:
            return ()
        if isinstance(self.quote_condition, tuple):
            return self.quote_condition
        return (self.quote_condition,)

    def dte(
        self,
        today: date | datetime | None = None,
        *,
        as_of: date | datetime | None = None,
    ) -> int:
        """Calendar DTE measured on the New York exchange date.

        ``today`` remains for compatibility; new callers should pass ``as_of``
        so every component in a cycle uses the same clock value.
        """
        if today is not None and as_of is not None:
            raise ValueError("pass either today or as_of, not both")
        return (self.expiry - exchange_date(as_of if as_of is not None else today)).days


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


def underlying_snapshot(
    settings: Settings,
    symbol: str,
    *,
    raw_sink: Callable[[str, dict], None] | None = None,
) -> UnderlyingSnapshot | None:
    """Return the best available underlier observation with provenance.

    A present-but-timestamp-less latest trade remains the selected observation;
    strict entry callers will reject it rather than silently substituting a
    prior daily close that happens to have a timestamp.
    """
    data = get_json(
        f"{DATA_HOST}/v2/stocks/{symbol}/snapshot",
        settings.auth_headers,
        {"feed": "iex"},
    )
    if raw_sink is not None:
        raw_sink(symbol, data)
    sources = (
        ("latestTrade", "latest_trade", "p"),
        ("minuteBar", "minute_bar", "c"),
        ("dailyBar", "daily_bar", "c"),
        ("prevDailyBar", "previous_daily_bar", "c"),
    )
    for key, source, price_key in sources:
        node = data.get(key) or {}
        price = node.get(price_key)
        try:
            parsed_price = float(price)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(parsed_price) or parsed_price <= 0:
            continue
        return UnderlyingSnapshot(
            symbol=symbol,
            price=parsed_price,
            timestamp=_parse_timestamp(node.get("t")),
            source=source,
        )
    return None


def underlying_snapshot_is_fresh(
    snapshot: UnderlyingSnapshot | None,
    settings: Settings,
    as_of: datetime,
) -> bool:
    """Whether an underlier mark is suitable for an open-session decision."""
    if snapshot is None or snapshot.source not in {"latest_trade", "minute_bar"}:
        return False
    if not math.isfinite(snapshot.price) or snapshot.price <= 0:
        return False
    if snapshot.timestamp is None:
        return False
    try:
        observed = _aware_utc(snapshot.timestamp, field="underlying timestamp")
        reference = _aware_utc(as_of, field="as_of")
    except (TypeError, ValueError):
        return False
    age = (reference - observed).total_seconds()
    max_age = float(getattr(
        settings.risk,
        "max_underlying_age_seconds",
        settings.risk.max_quote_age_seconds,
    ))
    max_future_skew = float(getattr(
        settings.risk, "max_future_quote_skew_seconds", 2.0,
    ))
    return -max_future_skew <= age <= max_age


def underlying_price(
    settings: Settings,
    symbol: str,
    *,
    as_of: datetime | None = None,
    require_fresh: bool = False,
    raw_sink: Callable[[str, dict], None] | None = None,
) -> float | None:
    """Compatibility wrapper returning a timestamp-validated IEX mark."""
    observed = underlying_snapshot(settings, symbol, raw_sink=raw_sink)
    if observed is None:
        return None
    if require_fresh:
        if as_of is None:
            raise ValueError("strict underlying price requires as_of")
        if not underlying_snapshot_is_fresh(observed, settings, as_of):
            return None
    return observed.price


STANDARD_MULTIPLIER = 100
STANDARD_DELIVERABLE_AMOUNT = 100.0


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    parsed = _decimal_or_none(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (InvalidOperation, ValueError):
        return None


def _deliverables_from_master(rows: object) -> tuple[OptionDeliverable, ...] | None:
    if not isinstance(rows, (list, tuple)):
        return None
    parsed: list[OptionDeliverable] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = _decimal_or_none(row.get("amount"))
        allocation = _decimal_or_none(row.get("allocation_percentage"))
        parsed.append(OptionDeliverable(
            type=str(row.get("type") or ""),
            symbol=str(row.get("symbol") or ""),
            amount=float(amount) if amount is not None else float("nan"),
            allocation_percentage=(
                float(allocation) if allocation is not None else float("nan")
            ),
            settlement_type=_optional_text(row.get("settlement_type")),
            settlement_method=_optional_text(row.get("settlement_method")),
            delayed_settlement=(
                bool(row["delayed_settlement"])
                if row.get("delayed_settlement") is not None else None
            ),
        ))
    return tuple(parsed)


def _with_contract_metadata(contract: Contract, row: object) -> Contract | None:
    """Merge one Alpaca option-contract master row onto a quote-derived Contract.

    Returns None when the master DISAGREES with the OCC symbol we parsed. A
    mismatch on underlying, expiry, type or strike means the two records are not
    describing the same instrument, and silently preferring either one would let
    an order reference a contract we never priced.

    A row that agrees but is non-standard (adjusted deliverables, a mini
    multiplier, a missing size) still merges and is still returned: the metadata
    is useful for diagnostics. ``standard_contract_is_verified`` is what decides
    whether it may become a trade.
    """
    if not isinstance(row, dict):
        return None

    underlying = _optional_text(row.get("underlying_symbol"))
    if underlying is not None and underlying != contract.underlying:
        return None

    expiry_text = _optional_text(row.get("expiration_date"))
    if expiry_text is not None:
        try:
            if date.fromisoformat(expiry_text) != contract.expiry:
                return None
        except ValueError:
            return None

    kind_text = (_optional_text(row.get("type")) or "").lower()
    if kind_text:
        expected = {"call": "C", "put": "P"}.get(kind_text)
        if expected is None or expected != contract.kind:
            return None

    strike = _decimal_or_none(row.get("strike_price"))
    if strike is not None and strike != _decimal_or_none(contract.strike):
        return None

    tradable = row.get("tradable")
    return replace(
        contract,
        multiplier=_int_or_none(row.get("multiplier")),
        contract_size=_int_or_none(row.get("size")),
        contract_status=_optional_text(row.get("status")),
        contract_tradable=bool(tradable) if tradable is not None else None,
        contract_style=_optional_text(row.get("style")),
        root_symbol=_optional_text(row.get("root_symbol")),
        deliverables=_deliverables_from_master(row.get("deliverables")),
        metadata_verified=True,
    )


def standard_contract_is_verified(contract: Contract | None) -> bool:
    """True only for a master-matched, standard 100-share equity deliverable.

    Everything here is a reason an option cannot be sized with the ordinary
    ``price x 100 x contracts`` arithmetic the rest of the system assumes:

    * no master row at all - we never confirmed what the contract delivers;
    * multiplier or size other than 100 - a mini or adjusted contract;
    * a root symbol differing from the underlier, or more than one deliverable,
      or a non-equity/partial deliverable - the classic post-corporate-action
      adjusted series, where exercise no longer delivers 100 ordinary shares.

    Fail closed: an unverifiable contract is not tradeable.
    """
    if contract is None or not contract.metadata_verified:
        return False
    if contract.multiplier != STANDARD_MULTIPLIER:
        return False
    if contract.contract_size != STANDARD_MULTIPLIER:
        return False
    if contract.contract_tradable is False:
        return False
    status = (contract.contract_status or "active").lower()
    if status != "active":
        return False
    if contract.root_symbol is not None and contract.root_symbol != contract.underlying:
        return False

    deliverables = contract.deliverables
    if not deliverables or len(deliverables) != 1:
        return False
    only = deliverables[0]
    if only.type.lower() != "equity" or only.symbol != contract.underlying:
        return False
    if not math.isfinite(only.amount) or only.amount != STANDARD_DELIVERABLE_AMOUNT:
        return False
    if not math.isfinite(only.allocation_percentage):
        return False
    return only.allocation_percentage == 100.0


def _fetch_contract_master(
    settings: Settings, underlying: str, *, expiry_gte: date, expiry_lte: date,
) -> dict[str, dict]:
    """Page Alpaca's option-contract master for one underlier."""
    rows: dict[str, dict] = {}
    page_token: str | None = None
    while True:
        payload = get_json(
            f"{PAPER_HOST}/v2/options/contracts",
            settings.auth_headers,
            {
                "underlying_symbols": underlying,
                "status": "active",
                "show_deliverables": "true",
                "expiration_date_gte": expiry_gte.isoformat(),
                "expiration_date_lte": expiry_lte.isoformat(),
                "limit": 10000,
                "page_token": page_token,
            },
        )
        for row in payload.get("option_contracts") or []:
            symbol = _optional_text(row.get("symbol"))
            if symbol:
                rows[symbol] = row
        page_token = payload.get("next_page_token")
        if not page_token:
            break
    return rows


def option_chain(
    settings: Settings,
    underlying: str,
    *,
    spot: float,
    strike_band_pct: float = 0.10,
    limit: int = 500,
    as_of: date | datetime | None = None,
    raw_sink: Callable[[str, dict], None] | None = None,
) -> list[Contract]:
    """Fetch a live option chain, strike- and expiry-bounded.

    Two defences, both from measured failures:

    1. ``expiration_date_gte`` is ALWAYS sent. Without it the endpoint returns
       EXPIRED contracts - we observed Aug 31 contracts returned on Sep 1.
    2. Contracts with null greeks are DROPPED, never defaulted to zero. Expired
       contracts return ``greeks: null``; coercing that to 0.0 would size a
       position off a delta of zero.
    """
    today = exchange_date(as_of)
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
        page_snapshots = payload.get("snapshots") or {}
        if raw_sink is not None:
            # Capture before parsing/filtering: historical option quotes,
            # greeks and IV cannot be fetched again from this endpoint.
            raw_sink(underlying, page_snapshots)
        for symbol, snapshot in page_snapshots.items():
            contract = _to_contract(symbol, snapshot)
            if contract is not None:
                contracts.append(contract)
        page_token = payload.get("next_page_token")
        if not page_token:
            break

    if not contracts:
        return contracts

    # A snapshot proves a price existed; it does not prove what the contract
    # delivers. Adjusted and mini series quote perfectly normally and would be
    # sized with the wrong multiplier all the way to submission, so every quote
    # is matched against the contract master and anything not a standard
    # 100-share equity deliverable is dropped here rather than later.
    master = _fetch_contract_master(
        settings,
        underlying,
        expiry_gte=today + timedelta(days=budget.min_dte),
        expiry_lte=today + timedelta(days=budget.max_dte),
    )
    verified: list[Contract] = []
    for contract in contracts:
        enriched = _with_contract_metadata(contract, master.get(contract.symbol))
        if enriched is not None and standard_contract_is_verified(enriched):
            verified.append(enriched)
    return verified


def option_snapshots(
    settings: Settings,
    symbols: Iterable[str],
    *,
    raw_sink: Callable[[str, dict], None] | None = None,
    verify_contracts: bool = True,
) -> dict[str, Contract]:
    """Refresh exact option contracts immediately before an order decision.

    The multi-snapshot endpoint accepts at most 100 comma-separated symbols.
    This execution-path helper is deliberately all-or-nothing: a missing or
    unusable leg means the spread can no longer be priced as one structure.
    """
    requested = list(dict.fromkeys(str(symbol) for symbol in symbols))
    if not requested:
        raise ValueError("at least one option symbol is required")
    if len(requested) > 100:
        raise ValueError("option snapshot endpoint accepts at most 100 symbols")

    payload = get_json(
        f"{DATA_HOST}/v1beta1/options/snapshots",
        settings.auth_headers,
        {"symbols": ",".join(requested), "feed": "indicative"},
    )
    snapshots = payload.get("snapshots") or {}
    if raw_sink is not None:
        raw_sink("selected", snapshots)

    parsed = {
        symbol: contract
        for symbol in requested
        if (contract := _to_contract(symbol, snapshots.get(symbol) or {})) is not None
    }
    missing = [symbol for symbol in requested if symbol not in parsed]
    if missing:
        raise ValueError(f"missing option snapshots for {missing}")

    # Re-match each leg to its exact master row. This runs immediately before an
    # order decision, so it is deliberately all-or-nothing: if any leg cannot be
    # confirmed as a standard contract, the spread can no longer be sized as one
    # structure and no part of it may be submitted.
    if not verify_contracts:
        # Exit path. Contract-master verification is an ENTRY gate: it decides
        # whether an option can be sized with standard 100-share arithmetic. A
        # position that became non-standard through a corporate action must
        # still be closable, so refusing to quote it here would strand real
        # risk in the book. Pricing still requires an exact, fresh, executable
        # quote per leg - see exits.price_exit_plan.
        return parsed

    verified: dict[str, Contract] = {}
    unverified: list[str] = []
    for symbol, contract in parsed.items():
        row = get_json(
            f"{PAPER_HOST}/v2/options/contracts/{symbol}",
            settings.auth_headers,
            {"show_deliverables": "true"},
        )
        enriched = _with_contract_metadata(contract, row)
        if enriched is None or not standard_contract_is_verified(enriched):
            unverified.append(symbol)
            continue
        verified[symbol] = enriched
    if unverified:
        raise ValueError(
            f"unverified or nonstandard option contracts: {unverified}"
        )
    return verified


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
        quote_timestamp=_parse_timestamp(quote.get("t")),
        bid_size=_parse_size(quote.get("bs")),
        ask_size=_parse_size(quote.get("as")),
        bid_exchange=_optional_text(quote.get("bx")),
        ask_exchange=_optional_text(quote.get("ax")),
        quote_condition=_parse_condition(quote.get("c")),
    )


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_size(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_condition(value: object) -> str | tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return str(value)


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def cross_leg_quote_skew_seconds(contracts: Iterable[Contract]) -> float | None:
    """Return the newest-minus-oldest quote timestamp across all legs.

    ``None`` means the skew is unknowable because no legs were supplied or at
    least one leg lacks a timestamp.  It must not be interpreted as zero.
    """
    legs = list(contracts)
    if not legs or any(leg.quote_timestamp is None for leg in legs):
        return None
    timestamps = [
        _aware_utc(leg.quote_timestamp, field="quote_timestamp")
        for leg in legs
        if leg.quote_timestamp is not None
    ]
    return (max(timestamps) - min(timestamps)).total_seconds()


def quotes_are_synchronous(
    contracts: Iterable[Contract],
    settings: Settings | None = None,
    *,
    max_skew_seconds: float | None = None,
) -> bool:
    """Whether every leg has a timestamp within the allowed cross-leg skew."""
    if max_skew_seconds is None:
        if settings is None:
            max_skew_seconds = 2.0
        else:
            max_skew_seconds = float(
                getattr(
                    settings.risk,
                    "max_leg_quote_skew_seconds",
                    getattr(settings.risk, "max_cross_leg_quote_skew_seconds", 2.0),
                )
            )
    if max_skew_seconds < 0:
        raise ValueError("max_skew_seconds must be non-negative")
    skew = cross_leg_quote_skew_seconds(contracts)
    return skew is not None and skew <= max_skew_seconds


def tradeable(
    contract: Contract,
    settings: Settings,
    *,
    as_of: datetime | None = None,
) -> bool:
    """Liquidity and, when ``as_of`` is supplied, quote-freshness filter.

    Omitting ``as_of`` preserves the original bid/spread behavior for callers
    that do not yet carry one cycle timestamp.  Supplying it opts into strict,
    fail-closed timestamp and displayed-size checks.
    """
    budget = settings.risk
    numeric = (
        contract.bid, contract.ask, contract.delta, contract.gamma,
        contract.theta, contract.vega, contract.iv, contract.strike,
    )
    if not all(math.isfinite(float(value)) for value in numeric):
        return False
    if contract.ask < contract.bid or contract.ask <= 0 or contract.iv <= 0:
        return False
    dte = contract.dte(as_of=as_of) if as_of is not None else contract.dte()
    if dte < budget.min_dte or dte > budget.max_dte:
        return False
    if contract.bid < budget.min_bid:
        return False
    if contract.spread_pct > budget.max_quote_spread_pct:
        return False
    if as_of is None:
        # Legacy diagnostic path: callers without a cycle timestamp still get
        # the original liquidity behavior and may inspect synthetic quotes.
        return True

    # Entry path. An option whose deliverable we never confirmed cannot be
    # sized, so it is not tradeable no matter how good the quote looks.
    if not standard_contract_is_verified(contract):
        return False

    reference = _aware_utc(as_of, field="as_of")
    if contract.quote_timestamp is None:
        return False
    try:
        quote_time = _aware_utc(contract.quote_timestamp, field="quote_timestamp")
    except (TypeError, ValueError):
        return False

    max_age = float(getattr(budget, "max_quote_age_seconds", 120.0))
    max_future_skew = float(getattr(budget, "max_future_quote_skew_seconds", 2.0))
    age = (reference - quote_time).total_seconds()
    if age > max_age or age < -max_future_skew:
        return False

    min_size = int(getattr(budget, "min_quote_size", 1))
    if contract.bid_size is None or contract.ask_size is None:
        return False
    if contract.bid_size < min_size or contract.ask_size < min_size:
        return False
    return True
