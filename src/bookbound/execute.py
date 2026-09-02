"""Order execution through the Alpaca CLI.

The hackathon requires the MCP server or the CLI. Execution goes through the
CLI's raw passthrough, verified working on 2026-09-01:
    `alpaca api POST /v2/orders` accepts order_class "mleg".
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .exits import (
    QUOTE_NATURAL_LIMIT_SOURCE,
    ExitLeg,
    ExitOrder,
    ExitPlan,
    price_exit_plan,
)
from .config import Settings
from .market import Contract, parse_occ
from .structures import Structure


class ExecutionError(RuntimeError):
    pass


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None
    client_order_id: str
    status: str
    payload: dict
    error: str | None = None


@dataclass
class CancelResult:
    ok: bool
    order_id: str
    status: str
    error: str | None = None


def cli_available() -> bool:
    return shutil.which("alpaca") is not None


def _cli_environment(settings: Settings) -> dict[str, str]:
    """Bind CLI writes to the exact credentials/environment used for reads."""
    environment = dict(os.environ)
    environment.update({
        "ALPACA_API_KEY": settings.api_key,
        "ALPACA_SECRET_KEY": settings.secret_key,
        "ALPACA_LIVE_TRADE": "false" if settings.paper else "true",
        "ALPACA_QUIET": "1",
    })
    return environment


def build_mleg_payload(structure: Structure, *, client_order_id: str) -> dict:
    """Build the MLEG order body.

    Constraints encoded here are all verified against the live API:
      - ratio_qty must be relatively prime (GCD = 1), enforced server-side
      - market orders are rejected outside market hours, so we always use limit
      - no equity legs are permitted in an mleg order
    """
    return {
        "order_class": "mleg",
        "qty": str(structure.qty),
        "type": "limit",
        "limit_price": f"{structure.limit_price():.2f}",
        "time_in_force": "day",
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": structure.long.symbol,
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
            {
                "symbol": structure.short.symbol,
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
        ],
    }


def strategy_client_order_id(structure: Structure, trading_date: date) -> str:
    """Stable per-session identity for one exact deterministic structure."""
    digest = hashlib.sha256(
        f"{trading_date.isoformat()}|{structure.key}".encode("utf-8")
    ).hexdigest()[:20]
    return f"bb-{trading_date:%Y%m%d}-{digest}"


def close_client_order_id(
    plan: ExitPlan,
    trading_date: date,
    *,
    revision: int = 0,
) -> str:
    """Stable identity for one exact close target and explicit price revision.

    Retries of the same intent reuse the broker idempotency key.  A caller that
    has confirmed the previous order terminal and intentionally reprices must
    increment ``revision``; changing a quote alone never silently creates a
    second close order.
    """
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise ValueError("close revision must be a non-negative integer")
    legs = "|".join(
        f"{leg.symbol}:{leg.qty}:{leg.side}:{leg.position_intent}"
        for leg in sorted(plan.legs, key=lambda item: item.symbol)
    )
    identity = (
        f"{trading_date.isoformat()}|{plan.underlying}|"
        f"{plan.expiry.isoformat()}|{plan.kind}|{legs}|r{revision}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"bb-exit-{trading_date:%Y%m%d}-{digest}"


def _order_result_from_process(
    process,
    *,
    client_order_id: str,
    payload: dict,
) -> OrderResult:
    """Convert a CLI response into a conservative broker acknowledgement."""
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    if process.returncode != 0:
        return OrderResult(
            False, None, client_order_id, "CLI_ERROR", payload,
            error=(stderr or stdout)[:400],
        )
    raw = stdout or stderr
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        return OrderResult(
            False, None, client_order_id, "UNPARSEABLE", payload,
            error=raw[:400],
        )
    if not isinstance(response, dict):
        return OrderResult(
            False, None, client_order_id, "UNPARSEABLE", payload,
            error=raw[:400],
        )
    if response.get("error") or response.get("code"):
        return OrderResult(
            False, None, client_order_id, "REJECTED", payload,
            error=json.dumps(response)[:400],
        )
    order_id = response.get("id")
    status = str(response.get("status") or "").lower()
    terminal_failure = {
        "rejected", "canceled", "cancelled", "expired", "suspended", "stopped"
    }
    accepted = {
        "accepted", "new", "pending_new", "accepted_for_bidding",
        "partially_filled", "filled", "pending_replace", "replaced",
    }
    if status in terminal_failure:
        return OrderResult(
            False, order_id, client_order_id, status.upper(), payload,
            error=json.dumps(response)[:400],
        )
    if not order_id:
        return OrderResult(
            False, None, client_order_id, "MISSING_ID", payload,
            error=json.dumps(response)[:400],
        )
    if status not in accepted:
        return OrderResult(
            False, order_id, client_order_id, "UNEXPECTED_STATUS", payload,
            error=json.dumps(response)[:400],
        )
    return OrderResult(True, order_id, client_order_id, status, payload)


def _validate_close_leg(leg: ExitLeg) -> None:
    if not isinstance(leg.qty, int) or isinstance(leg.qty, bool) or leg.qty <= 0:
        raise ExecutionError(f"close quantity must be a positive integer: {leg.qty!r}")
    expected_intent = {
        "buy": "buy_to_close",
        "sell": "sell_to_close",
    }.get(leg.side)
    if expected_intent is None:
        raise ExecutionError(f"invalid close side for {leg.symbol}: {leg.side!r}")
    if leg.position_intent != expected_intent:
        raise ExecutionError(
            f"{leg.symbol}: side {leg.side!r} requires {expected_intent!r}, "
            f"got {leg.position_intent!r}"
        )


def _validate_close_plan(plan: ExitPlan) -> None:
    if not plan.legs:
        raise ExecutionError("a close plan must contain at least one leg")
    if len(plan.legs) > 4:
        raise ExecutionError(
            "Alpaca MLEG supports at most four legs; refusing to decompose an "
            "atomic close plan"
        )
    symbols = [leg.symbol for leg in plan.legs]
    if len(set(symbols)) != len(symbols):
        raise ExecutionError("a close plan cannot contain duplicate option symbols")

    for leg in plan.legs:
        _validate_close_leg(leg)
        parsed = parse_occ(leg.symbol)
        if parsed is None:
            raise ExecutionError(f"invalid OCC option symbol in close plan: {leg.symbol}")
        underlying, expiry, kind, _ = parsed
        if (underlying, expiry, kind) != (plan.underlying, plan.expiry, plan.kind):
            raise ExecutionError(
                f"{leg.symbol} does not belong to close-plan series "
                f"{plan.underlying}/{plan.expiry.isoformat()}/{plan.kind}"
            )

    if (
        plan.limit_price is None
        or not math.isfinite(plan.limit_price)
        or plan.limit_price == 0
        or plan.limit_source != QUOTE_NATURAL_LIMIT_SOURCE
        or plan.limit_as_of is None
        or plan.limit_as_of.tzinfo is None
        or plan.limit_as_of.utcoffset() is None
    ):
        raise ExecutionError(
            "close plan has no verified quote-derived limit; refusing an "
            "unbounded order"
        )


def build_close_payload(plan: ExitPlan, *, client_order_id: str) -> dict:
    """Build one broker payload for a structure-level close plan.

    A healthy vertical becomes one MLEG order, never two independent orders.
    The order's base quantity is the GCD of total leg quantities and each
    ``ratio_qty`` is divided by that GCD, satisfying Alpaca's relatively-prime
    ratio requirement without changing the number of contracts closed.

    A one-leg payload is permitted only because reconciliation sometimes starts
    with a position that is already naked.  It remains an explicit close-only
    order and cannot add risk.
    """
    _validate_close_plan(plan)
    if len(plan.legs) == 1:
        leg = plan.legs[0]
        return {
            "symbol": leg.symbol,
            "qty": str(leg.qty),
            "side": leg.side,
            "type": "limit",
            "limit_price": f"{abs(plan.limit_price):.2f}",
            "time_in_force": "day",
            "position_intent": leg.position_intent,
            "client_order_id": client_order_id,
        }

    base_qty = math.gcd(*(leg.qty for leg in plan.legs))
    return {
        "order_class": "mleg",
        "qty": str(base_qty),
        "type": "limit",
        "limit_price": f"{plan.limit_price:.2f}",
        "time_in_force": "day",
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": leg.symbol,
                "ratio_qty": str(leg.qty // base_qty),
                "side": leg.side,
                "position_intent": leg.position_intent,
            }
            for leg in plan.legs
        ],
    }


def submit(
    structure: Structure,
    *,
    dry_run: bool = False,
    client_order_id: str | None = None,
    trading_date: date | None = None,
    settings: Settings | None = None,
) -> OrderResult:
    """Submit the structure with a repeatable strategy/session identity."""
    client_order_id = client_order_id or strategy_client_order_id(
        structure, trading_date or date.today()
    )
    payload = build_mleg_payload(structure, client_order_id=client_order_id)

    if dry_run:
        return OrderResult(True, None, client_order_id, "DRY_RUN", payload)

    if settings is None:
        raise ExecutionError("live execution requires bound Settings credentials")
    if not cli_available():
        raise ExecutionError("alpaca CLI not found; brew install alpacahq/tap/cli")

    # The CLI writes error JSON to stderr, so both streams are captured.
    process = subprocess.run(
        ["alpaca", "api", "POST", "/v2/orders"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=45,
        env=_cli_environment(settings),
    )
    return _order_result_from_process(
        process, client_order_id=client_order_id, payload=payload,
    )


def cancel_all(settings: Settings | None = None) -> str:
    """Kill switch."""
    if not cli_available():
        raise ExecutionError("alpaca CLI not found")
    if settings is None:
        raise ExecutionError("live execution requires bound Settings credentials")
    process = subprocess.run(["alpaca", "order", "cancel-all"],
                             capture_output=True, text=True, timeout=45,
                             env=_cli_environment(settings))
    return (process.stdout or process.stderr or "").strip()


def _parse_broker_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def stale_opening_orders(
    orders: list[dict],
    *,
    as_of: datetime,
    max_age_seconds: float,
) -> list[dict]:
    """Return working entry orders old enough to require cancel/replace.

    Closing orders are never canceled by entry hygiene.  Missing or ambiguous
    creation times are treated as stale, because an order with unknowable age
    must not remain an invisible open-ended commitment.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of datetime must be timezone-aware")
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    reference = as_of.astimezone(timezone.utc)
    terminal = {
        "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"
    }
    stale: list[dict] = []
    for order in orders:
        if str(order.get("status") or "").lower() in terminal:
            continue
        legs = order.get("legs") or []
        intents = [
            str(leg.get("position_intent") or "")
            for leg in legs if isinstance(leg, dict)
        ]
        parent_intent = str(order.get("position_intent") or "")
        is_opening = parent_intent.endswith("_to_open") or any(
            intent.endswith("_to_open") for intent in intents
        )
        if not is_opening:
            continue
        created = _parse_broker_timestamp(order.get("created_at"))
        if created is None or (reference - created).total_seconds() > max_age_seconds:
            stale.append(order)
    return sorted(
        stale,
        key=lambda order: str(order.get("id") or order.get("client_order_id") or ""),
    )


def cancel_order(
    order_id: str,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> CancelResult:
    """Cancel one broker order; used before considering replacement entries."""
    if not order_id:
        return CancelResult(False, order_id, "INVALID", "missing broker order id")
    if dry_run:
        return CancelResult(True, order_id, "DRY_RUN")
    if settings is None:
        raise ExecutionError("live execution requires bound Settings credentials")
    if not cli_available():
        raise ExecutionError("alpaca CLI not found")
    process = subprocess.run(
        ["alpaca", "api", "DELETE", f"/v2/orders/{order_id}"],
        capture_output=True, text=True, timeout=45,
        env=_cli_environment(settings),
    )
    raw = (process.stdout or "") + (process.stderr or "")
    if process.returncode != 0:
        return CancelResult(False, order_id, "REJECTED", raw[:400])
    return CancelResult(True, order_id, "CANCELED")


def close_position(
    order: ExitOrder,
    *,
    quote: Contract | None = None,
    as_of: datetime | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    client_order_id: str | None = None,
    trading_date: date | None = None,
    revision: int = 0,
) -> OrderResult:
    """Compatibility wrapper for a quote-bounded, close-only single leg."""
    if settings is None:
        raise ExecutionError("bounded close pricing requires Settings")
    if quote is None or as_of is None:
        raise ExecutionError(
            "single-leg close requires an exact quote and explicit as_of"
        )
    parsed = parse_occ(order.symbol)
    if parsed is None:
        raise ExecutionError(f"invalid OCC option symbol: {order.symbol}")
    underlying, expiry, kind, _ = parsed
    plan = price_exit_plan(
        ExitPlan(
            underlying=underlying,
            expiry=expiry,
            kind=kind,
            legs=(ExitLeg(
                symbol=order.symbol,
                qty=order.qty,
                side=order.side,
                position_intent=order.position_intent,
            ),),
            reason=order.reason,
            detail=order.detail,
        ),
        {order.symbol: quote},
        settings,
        as_of=as_of,
    )
    return close_structure(
        plan,
        dry_run=dry_run,
        settings=settings,
        client_order_id=client_order_id,
        trading_date=trading_date,
        revision=revision,
    )


def close_structure(
    plan: ExitPlan,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
    client_order_id: str | None = None,
    trading_date: date | None = None,
    revision: int = 0,
) -> OrderResult:
    """Submit a close plan as one order.

    Balanced verticals are sent as MLEG orders, which prevents one successful
    leg order from turning the remaining short leg into undefined risk.  This
    function performs no fallback to independent leg orders if MLEG submission
    fails.
    """
    if client_order_id is None:
        identity_date = trading_date or (
            plan.limit_as_of.date() if plan.limit_as_of is not None else date.today()
        )
        client_order_id = close_client_order_id(
            plan, identity_date, revision=revision,
        )
    payload = build_close_payload(plan, client_order_id=client_order_id)
    if dry_run:
        return OrderResult(True, None, client_order_id, "DRY_RUN", payload)
    if settings is None:
        raise ExecutionError("live execution requires bound Settings credentials")
    if not cli_available():
        raise ExecutionError("alpaca CLI not found")

    process = subprocess.run(
        ["alpaca", "api", "POST", "/v2/orders"],
        input=json.dumps(payload), capture_output=True, text=True, timeout=45,
        env=_cli_environment(settings),
    )
    return _order_result_from_process(
        process, client_order_id=client_order_id, payload=payload,
    )


# Explicit aliases for integration code: all retain the one-plan/one-order
# guarantee and never fall back to leg-by-leg execution.
close_exit_plan = close_structure
submit_close = close_structure
