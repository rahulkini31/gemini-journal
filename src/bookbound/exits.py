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

from dataclasses import dataclass
from datetime import date

from .config import Settings
from .market import Contract, parse_occ


@dataclass(frozen=True)
class ExitOrder:
    """An instruction to close one open option position."""

    symbol: str
    qty: int                 # positive; direction comes from `side`
    side: str                # "buy" closes a short, "sell" closes a long
    position_intent: str
    reason: str
    detail: str


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
