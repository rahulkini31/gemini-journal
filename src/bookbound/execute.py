"""Order execution through the Alpaca CLI.

The hackathon requires the MCP server or the CLI. Execution goes through the
CLI's raw passthrough, verified working on 2026-09-01:
    `alpaca api POST /v2/orders` accepts order_class "mleg".
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass

from .exits import ExitOrder
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


def cli_available() -> bool:
    return shutil.which("alpaca") is not None


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


def submit(structure: Structure, *, dry_run: bool = False) -> OrderResult:
    """Submit the structure. Idempotent by client_order_id."""
    if not cli_available():
        raise ExecutionError("alpaca CLI not found; brew install alpacahq/tap/cli")

    client_order_id = f"bb-{uuid.uuid4()}"
    payload = build_mleg_payload(structure, client_order_id=client_order_id)

    if dry_run:
        return OrderResult(True, None, client_order_id, "DRY_RUN", payload)

    # The CLI writes error JSON to stderr, so both streams are captured.
    process = subprocess.run(
        ["alpaca", "api", "POST", "/v2/orders"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=45,
    )
    raw = (process.stdout or "") + (process.stderr or "")
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        return OrderResult(False, None, client_order_id, "UNPARSEABLE", payload,
                           error=raw[:400])

    if response.get("error") or response.get("code"):
        return OrderResult(False, None, client_order_id, "REJECTED", payload,
                           error=json.dumps(response)[:400])

    return OrderResult(True, response.get("id"), client_order_id,
                       response.get("status", "unknown"), payload)


def cancel_all() -> str:
    """Kill switch."""
    if not cli_available():
        raise ExecutionError("alpaca CLI not found")
    process = subprocess.run(["alpaca", "order", "cancel-all"],
                             capture_output=True, text=True, timeout=45)
    return (process.stdout or process.stderr or "").strip()


def close_position(order: ExitOrder, *, dry_run: bool = False) -> OrderResult:
    """Close one option leg with a marketable order.

    Exits use market orders during session hours: a stop-loss that does not fill
    is not a stop-loss. Options market orders are rejected outside market hours
    (verified: 42210000), so the caller must only invoke this while open.
    """
    if not cli_available():
        raise ExecutionError("alpaca CLI not found")

    client_order_id = f"bb-exit-{uuid.uuid4()}"
    payload = {
        "symbol": order.symbol,
        "qty": str(order.qty),
        "side": order.side,
        "type": "market",
        "time_in_force": "day",
        "position_intent": order.position_intent,
        "client_order_id": client_order_id,
    }
    if dry_run:
        return OrderResult(True, None, client_order_id, "DRY_RUN", payload)

    process = subprocess.run(
        ["alpaca", "api", "POST", "/v2/orders"],
        input=json.dumps(payload), capture_output=True, text=True, timeout=45,
    )
    raw = (process.stdout or "") + (process.stderr or "")
    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        return OrderResult(False, None, client_order_id, "UNPARSEABLE", payload,
                           error=raw[:400])
    if response.get("error") or response.get("code"):
        return OrderResult(False, None, client_order_id, "REJECTED", payload,
                           error=json.dumps(response)[:400])
    return OrderResult(True, response.get("id"), client_order_id,
                       response.get("status", "unknown"), payload)
