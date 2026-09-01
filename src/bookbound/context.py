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
from datetime import date, datetime, timedelta, timezone

from .config import DATA_HOST, Settings
from .http import ApiError, get_json


def daily_bars(settings: Settings, symbol: str, days: int = 30) -> list[dict]:
    """Recent daily bars from IEX. The free plan withholds the last 15 minutes,
    which is irrelevant for daily bars used as trend context."""
    start = (date.today() - timedelta(days=days * 2)).isoformat()
    payload = get_json(
        f"{DATA_HOST}/v2/stocks/{symbol}/bars",
        settings.auth_headers,
        {"timeframe": "1Day", "start": start, "limit": days, "feed": "iex"},
    )
    return payload.get("bars") or []


def price_context(bars: list[dict]) -> dict:
    """Trend, realised vol and range position - the inputs to a directional view."""
    closes = [float(b["c"]) for b in bars if b.get("c")]
    if len(closes) < 5:
        return {"available": False}

    last = closes[-1]
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
        "last_close": round(last, 2),
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
    }


def recent_news(settings: Settings, symbols: list[str], limit: int = 8) -> list[dict]:
    """Headlines only. Sending article bodies would blow the token budget for
    little gain, and the model is ranking a shortlist, not writing research."""
    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
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


def build(settings: Settings, spots: dict[str, float]) -> dict:
    """Assemble the context block handed to the reasoning layer."""
    per_symbol = {}
    for symbol in settings.universe:
        try:
            per_symbol[symbol] = price_context(daily_bars(settings, symbol))
        except ApiError as exc:
            per_symbol[symbol] = {"available": False, "error": str(exc)[:120]}
    return {
        "spot_prices": {k: round(v, 2) for k, v in spots.items()},
        "underlyings": per_symbol,
        "headlines": recent_news(settings, list(settings.universe)),
        "data_caveat": (
            "Option quotes come from Alpaca's free INDICATIVE feed: derived, not "
            "OPRA, with trades delayed 15 minutes. Greeks are Black-Scholes "
            "values computed from those quotes. Treat them as a ranking signal, "
            "not a pricing oracle. Underlying bars are IEX only."
        ),
    }
