"""Market context for the reasoning layer.

The adversary repeatedly vetoed trades for having "no directional thesis". That
was a fair objection to an unfair setup: the proposer was given equity, book
greeks and nothing else, so a directional view was not merely absent - it was
impossible to form.

This module supplies the evidence a directional claim can actually rest on:
recent price action, realised volatility, where spot sits in its recent range,
and current headlines. All from the free tier.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import DATA_HOST, Settings
from .http import ApiError, get_json


EXCHANGE_TIMEZONE = ZoneInfo("America/New_York")


def exchange_date(as_of: date | datetime | None = None) -> date:
    """Return the New York trading date for an explicit point in time.

    The process currently runs in Asia/Kolkata, whose calendar advances while
    the US session is still open.  Reading ``date.today()`` there can therefore
    shift every DTE bound by one day.  Naive datetimes are rejected because
    silently guessing their timezone would recreate the same ambiguity.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of datetime must be timezone-aware")
        return as_of.astimezone(EXCHANGE_TIMEZONE).date()
    if isinstance(as_of, date):
        return as_of
    raise TypeError("as_of must be a date, datetime, or None")


def _as_of_instant(as_of: date | datetime | None) -> datetime:
    """Normalize an as-of value for APIs that require a timestamp."""
    if as_of is None:
        return datetime.now(timezone.utc)
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of datetime must be timezone-aware")
        return as_of.astimezone(timezone.utc)
    if isinstance(as_of, date):
        # A date denotes the complete exchange day, not midnight at the start
        # of that day.  Convert its final instant to UTC for the market API.
        return datetime.combine(as_of, time.max, EXCHANGE_TIMEZONE).astimezone(
            timezone.utc
        )
    raise TypeError("as_of must be a date, datetime, or None")


def daily_bars(
    settings: Settings,
    symbol: str,
    days: int = 30,
    *,
    as_of: date | datetime | None = None,
) -> list[dict]:
    """Recent daily bars from IEX. The free plan withholds the last 15 minutes,
    which is irrelevant for daily bars used as trend context.

    Alpaca defaults to ascending order, so ``limit=days`` used to select the
    *oldest* rows after ``start``.  Request descending data to cap from the
    newest end, then restore chronological order for return calculations.
    """
    if days <= 0:
        return []
    instant = _as_of_instant(as_of)
    trading_date = exchange_date(instant)
    start = (trading_date - timedelta(days=days * 2)).isoformat()
    payload = get_json(
        f"{DATA_HOST}/v2/stocks/{symbol}/bars",
        settings.auth_headers,
        {
            "timeframe": "1Day",
            "start": start,
            "end": instant.isoformat(),
            "limit": days,
            "feed": "iex",
            "sort": "desc",
            "adjustment": "all",
        },
    )
    bars = list(payload.get("bars") or [])
    return sorted(bars, key=lambda bar: str(bar.get("t") or ""))


def price_context(bars: list[dict], spot: float | None = None) -> dict:
    """Trend, realised vol and range position - the inputs to a directional view.

    ``spot`` is the LIVE price and must be supplied during the session. Daily
    bars end at the previous close, so a context built from bars alone describes
    yesterday's market. Measured live: bars put SPY at range position 0.968
    ("near its 20-day high") while spot was 762.02, a true position of 0.672 and
    down 1.84% on the day. The model was reasoning about the wrong market.
    """
    usable_bars = [bar for bar in bars if bar.get("c")]
    closes = [float(bar["c"]) for bar in usable_bars]
    if len(closes) < 5:
        return {"available": False}

    latest_bar_timestamp = usable_bars[-1].get("t")
    if isinstance(latest_bar_timestamp, datetime):
        latest_bar_timestamp = latest_bar_timestamp.isoformat()

    prev_close = closes[-1]
    last = float(spot) if spot else prev_close
    if spot:
        # treat live spot as the newest observation for every derived measure
        closes = closes + [last]
    returns = [
        (closes[i] / closes[i - 1] - 1.0)
        for i in range(1, len(closes))
        if closes[i - 1]
    ]
    realised_vol = (
        statistics.pstdev(returns) * (252 ** 0.5) if len(returns) > 1 else 0.0
    )
    window = closes[-20:]
    low, high = min(window), max(window)
    span = high - low

    def change(n: int) -> float | None:
        if len(closes) <= n or not closes[-1 - n]:
            return None
        return round((last / closes[-1 - n] - 1.0) * 100, 2)

    return {
        "available": True,
        "spot": round(last, 2),
        "prev_close": round(prev_close, 2),
        "is_live_spot": bool(spot),
        "change_today_pct": (
            round((last / prev_close - 1.0) * 100, 2) if spot and prev_close else None
        ),
        "change_1d_pct": change(1),
        "change_5d_pct": change(5),
        "change_20d_pct": change(20),
        "realised_vol_annualised": round(realised_vol, 4),
        "range_20d_low": round(low, 2),
        "range_20d_high": round(high, 2),
        "range_position": round((last - low) / span, 3) if span > 0 else None,
        "sma20_vs_last_pct": round(
            (last / statistics.fmean(window) - 1.0) * 100, 2
        ),
        "bars_used": len(closes),
        "latest_bar_timestamp": latest_bar_timestamp,
    }


def recent_news(
    settings: Settings,
    symbols: list[str],
    limit: int = 8,
    *,
    as_of: date | datetime | None = None,
) -> list[dict]:
    """Headlines only. Sending article bodies would blow the token budget for
    little gain, and the model is ranking a shortlist, not writing research."""
    since = (_as_of_instant(as_of) - timedelta(days=3)).isoformat()
    try:
        payload = get_json(
            f"{DATA_HOST}/v1beta1/news",
            settings.auth_headers,
            {"symbols": ",".join(symbols), "start": since, "limit": limit,
             "sort": "desc"},
        )
    except ApiError:
        return []
    return [
        {
            "headline": item.get("headline"),
            "symbols": item.get("symbols"),
            "created_at": item.get("created_at"),
        }
        for item in (payload.get("news") or [])
    ]


def build(
    settings: Settings,
    spots: dict[str, float],
    *,
    as_of: date | datetime | None = None,
) -> dict:
    """Assemble the context block handed to the reasoning layer."""
    instant = _as_of_instant(as_of)
    cycle_as_of: date | datetime = as_of if as_of is not None else instant
    per_symbol = {}
    for symbol in settings.universe:
        try:
            per_symbol[symbol] = price_context(
                daily_bars(settings, symbol, as_of=cycle_as_of),
                spot=spots.get(symbol),
            )
        except ApiError as exc:
            per_symbol[symbol] = {"available": False, "error": str(exc)[:120]}
    return {
        "as_of": instant.isoformat(),
        "exchange_date": exchange_date(instant).isoformat(),
        "spot_prices": {k: round(v, 2) for k, v in spots.items()},
        "underlyings": per_symbol,
        "headlines": recent_news(
            settings, list(settings.universe), as_of=cycle_as_of
        ),
        "data_caveat": (
            "Option quotes come from Alpaca's free INDICATIVE feed: derived, not "
            "OPRA, with trades delayed 15 minutes. Greeks are Black-Scholes "
            "values computed from those quotes. Treat them as a ranking signal, "
            "not a pricing oracle. Underlying bars are IEX only. Range position "
            "and realised vol include the LIVE spot, so change_today_pct is "
            "already reflected in them."
        ),
    }
