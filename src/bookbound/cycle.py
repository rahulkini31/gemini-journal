"""One full trading cycle: observe, construct, decide, gate, execute, reconcile."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import execute, reconcile
from .exits import evaluate_exits
from .agents import decide
from .audit import AuditLog
from .book import load_book, missing_greeks
from .config import Settings
from .market import Contract, market_clock, option_chain, underlying_price
from .risk import evaluate, shrink_to_fit
from .structures import (
    build_vertical_credit_spreads,
    build_vertical_debit_spreads,
)


@dataclass
class CycleReport:
    market_open: bool
    equity: float = 0.0
    book_greeks: dict = field(default_factory=dict)
    contracts_seen: int = 0
    candidates: int = 0
    exits: list = field(default_factory=list)
    outcome: str = "NO_TRADE"
    detail: str = ""
    verdict: dict | None = None
    order: dict | None = None
    health: dict = field(default_factory=dict)


def run_cycle(settings: Settings, *, dry_run: bool = True) -> CycleReport:
    audit = AuditLog(settings.audit_path)

    clock = market_clock(settings)
    if not clock.get("is_open"):
        audit.write("cycle_skipped", reason="market closed",
                    next_open=clock.get("next_open"))
        return CycleReport(market_open=False, detail="market closed",
                           outcome="MARKET_CLOSED")

    # --- observe -----------------------------------------------------------
    quotes: dict[str, Contract] = {}
    all_contracts: list[Contract] = []
    for symbol in settings.universe:
        spot = underlying_price(settings, symbol)
        if spot is None:
            continue
        chain = option_chain(settings, symbol, spot=spot)
        all_contracts.extend(chain)
        quotes.update({c.symbol: c for c in chain})

    book = load_book(settings, quotes)
    unpriced = missing_greeks(book, quotes)

    health = reconcile.summarise(book.raw_positions)
    if not health["healthy"]:
        audit.write("reconciliation_breach", **health)

    report = CycleReport(
        market_open=True,
        equity=book.equity,
        book_greeks=book.greeks.as_dict(),
        contracts_seen=len(all_contracts),
        health=health,
    )

    # --- manage exits first ------------------------------------------------
    # Freeing risk before considering new risk. An exit is never blocked by the
    # entry gates: closing a position always reduces exposure.
    for exit_order in evaluate_exits(book.raw_positions, quotes, settings):
        result = execute.close_position(exit_order, dry_run=dry_run)
        report.exits.append({
            "symbol": exit_order.symbol, "reason": exit_order.reason,
            "detail": exit_order.detail, "ok": result.ok,
            "status": result.status, "error": result.error,
        })
        audit.write("exit" if result.ok else "exit_failed",
                    symbol=exit_order.symbol, reason=exit_order.reason,
                    detail=exit_order.detail, side=exit_order.side,
                    qty=exit_order.qty, status=result.status,
                    error=result.error, dry_run=dry_run)
    if report.exits:
        # positions changed; re-read the book before sizing anything new
        book = load_book(settings, quotes)
        report.book_greeks = book.greeks.as_dict()

    # --- construct (deterministic, before any model runs) -------------------
    loss_cap = book.equity * settings.risk.max_loss_per_trade_pct
    candidates = []
    for kind in ("C", "P"):
        candidates.extend(build_vertical_debit_spreads(
            all_contracts, settings, kind=kind, max_loss_cap=loss_cap))
        candidates.extend(build_vertical_credit_spreads(
            all_contracts, settings, kind=kind, max_loss_cap=loss_cap))
    # keep only structures that already pass the gates - the model never sees
    # anything the risk engine would refuse
    admissible = [
        c for c in candidates
        if evaluate(c, book, settings, unpriced_positions=unpriced).admitted
    ]
    report.candidates = len(admissible)

    if not admissible:
        report.detail = (
            f"{len(candidates)} structures built, none passed the risk gates"
        )
        audit.write("no_trade", reason=report.detail,
                    book_greeks=report.book_greeks, equity=book.equity,
                    built=len(candidates))
        return report

    # --- decide (two models must agree) ------------------------------------
    context = {
        "equity": round(book.equity, 2),
        "book_greeks": report.book_greeks,
        "day_pnl_pct": round(book.day_pnl_pct, 4),
        "open_option_positions": health["option_positions"],
        "note": (
            "Quotes come from Alpaca's free INDICATIVE feed: derived, not OPRA, "
            "and trades are delayed 15 minutes. Greeks are Black-Scholes values "
            "computed by Alpaca from those quotes. Treat them as a ranking "
            "signal, not a pricing oracle."
        ),
    }
    decision = decide(admissible[:10], context, settings)

    if not decision.traded:
        report.outcome, report.detail = decision.outcome, decision.detail
        audit.write("no_trade", reason=decision.detail, outcome=decision.outcome,
                    proposer=decision.proposer, adversary=decision.adversary,
                    candidates=len(admissible))
        return report

    # --- gate again on the final size --------------------------------------
    structure = shrink_to_fit(decision.structure, book, settings)
    if structure is None:
        report.outcome, report.detail = "GATED", "no size fits the risk budget"
        audit.write("no_trade", reason=report.detail, key=decision.structure.key)
        return report

    verdict = evaluate(structure, book, settings, unpriced_positions=unpriced)
    report.verdict = {
        "admitted": verdict.admitted,
        "book_before": verdict.book_before,
        "book_after": verdict.book_after,
        "checks": verdict.checks,
    }
    if not verdict.admitted:
        report.outcome, report.detail = "GATED", "; ".join(verdict.reasons)
        audit.write("refused", structure=structure.summary(), **report.verdict)
        return report

    # --- execute -----------------------------------------------------------
    result = execute.submit(structure, dry_run=dry_run)
    report.order = {
        "ok": result.ok, "status": result.status, "order_id": result.order_id,
        "client_order_id": result.client_order_id, "error": result.error,
    }
    report.outcome = "SUBMITTED" if result.ok else "ORDER_REJECTED"
    report.detail = decision.detail

    audit.write(
        "order_submitted" if result.ok else "order_rejected",
        structure=structure.summary(),
        thesis=decision.proposer.get("thesis"),
        adversary=decision.adversary.get("reason"),
        book_before=verdict.book_before,
        book_after=verdict.book_after,
        order=report.order,
        dry_run=dry_run,
    )
    return report
