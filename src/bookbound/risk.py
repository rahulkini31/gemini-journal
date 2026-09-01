"""Deterministic risk gates. No LLM is ever consulted here.

Gates may VETO or SHRINK a candidate. They can never enlarge one, invent one,
or approve something the strategy layer did not propose.

Every rejection carries the bound that failed and the numbers on both sides of
it, because the refusal log is a deliverable, not a byproduct.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .book import Book, Greeks, greeks_of_legs
from .config import Settings
from .structures import Structure, execution_drag


@dataclass
class Verdict:
    """Outcome of the gate stack."""

    admitted: bool
    reasons: list[str] = field(default_factory=list)
    book_before: dict[str, float] = field(default_factory=dict)
    book_after: dict[str, float] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str, **numbers: float) -> None:
        self.checks.append(
            {"check": name, "passed": passed, "detail": detail, **numbers}
        )
        if not passed:
            self.reasons.append(f"{name}: {detail}")


def evaluate(
    structure: Structure,
    book: Book,
    settings: Settings,
    *,
    unpriced_positions: list[str] | None = None,
    pending_risk: float = 0.0,
) -> Verdict:
    """Admit or refuse a candidate structure based on the POST-TRADE book.

    ``pending_risk`` carries max-loss already committed earlier in this cycle but
    not yet reflected in ``book.raw_positions``. Without it, admitting several
    structures in one pass would each measure risk against a stale book.
    """
    budget = settings.risk
    verdict = Verdict(admitted=False)

    before = book.greeks
    after_book = book.with_added(structure.legs)
    after = after_book.greeks
    verdict.book_before = before.as_dict()
    verdict.book_after = after.as_dict()

    # --- fail closed on an unknown book -------------------------------------
    unpriced = unpriced_positions or []
    verdict.record(
        "book_fully_priced",
        not unpriced,
        "all open options priced" if not unpriced
        else f"cannot price held positions {unpriced}; book greeks unknown",
        unpriced_count=len(unpriced),
    )

    # --- circuit breaker ----------------------------------------------------
    drawdown = book.day_pnl_pct
    verdict.record(
        "daily_drawdown_halt",
        drawdown > -budget.daily_drawdown_halt_pct,
        f"day P&L {drawdown:+.2%} vs halt at {-budget.daily_drawdown_halt_pct:.2%}",
        day_pnl_pct=round(drawdown, 5),
    )

    # --- per-trade loss cap -------------------------------------------------
    per_trade_cap = book.equity * budget.max_loss_per_trade_pct
    verdict.record(
        "max_loss_per_trade",
        structure.max_loss <= per_trade_cap,
        f"max loss ${structure.max_loss:,.2f} vs cap ${per_trade_cap:,.2f}",
        max_loss=round(structure.max_loss, 2),
        cap=round(per_trade_cap, 2),
    )

    # --- aggregate open risk ------------------------------------------------
    open_risk = sum(
        abs(float(p.get("cost_basis") or 0.0))
        for p in book.raw_positions
        if p.get("asset_class") == "us_option"
    )
    open_risk += pending_risk
    total_cap = book.equity * budget.max_total_open_risk_pct
    verdict.record(
        "total_open_risk",
        open_risk + structure.max_loss <= total_cap,
        f"open risk ${open_risk:,.2f} (incl. ${pending_risk:,.2f} pending) "
        f"+ ${structure.max_loss:,.2f} vs cap ${total_cap:,.2f}",
        open_risk=round(open_risk, 2),
        pending=round(pending_risk, 2),
        cap=round(total_cap, 2),
    )

    # --- portfolio greek budget: the differentiator -------------------------
    verdict.record(
        "book_delta",
        abs(after.delta) <= budget.max_abs_delta,
        f"|delta| {abs(after.delta):,.1f} after (was {abs(before.delta):,.1f}) "
        f"vs budget {budget.max_abs_delta:,.1f}",
        before=round(before.delta, 2), after=round(after.delta, 2),
        budget=budget.max_abs_delta,
    )
    verdict.record(
        "book_vega",
        abs(after.vega) <= budget.max_abs_vega,
        f"|vega| {abs(after.vega):,.1f} after (was {abs(before.vega):,.1f}) "
        f"vs budget {budget.max_abs_vega:,.1f}",
        before=round(before.vega, 2), after=round(after.vega, 2),
        budget=budget.max_abs_vega,
    )
    verdict.record(
        "book_gamma",
        abs(after.gamma) <= budget.max_abs_gamma,
        f"|gamma| {abs(after.gamma):,.2f} after (was {abs(before.gamma):,.2f}) "
        f"vs budget {budget.max_abs_gamma:,.2f}",
        before=round(before.gamma, 3), after=round(after.gamma, 3),
        budget=budget.max_abs_gamma,
    )
    verdict.record(
        "book_theta",
        after.theta >= budget.min_theta,
        f"theta {after.theta:,.1f}/day after (was {before.theta:,.1f}) "
        f"vs floor {budget.min_theta:,.1f}",
        before=round(before.theta, 2), after=round(after.theta, 2),
        floor=budget.min_theta,
    )

    # --- edge erosion -------------------------------------------------------
    # Crossing a wide indicative quote can surrender more expected value than the
    # trade can plausibly make. Refuse when execution cost dominates the thesis.
    drag = execution_drag(structure)
    drag_cap = structure.max_loss * budget.max_edge_erosion_pct
    verdict.record(
        "edge_erosion",
        drag <= drag_cap,
        f"execution drag ${drag:,.2f} vs cap ${drag_cap:,.2f} "
        f"({budget.max_edge_erosion_pct:.0%} of max loss)",
        drag=round(drag, 2), cap=round(drag_cap, 2),
    )

    # --- concentration ------------------------------------------------------
    counts = book.positions_by_underlying()
    existing = counts.get(structure.underlying, 0)
    verdict.record(
        "concentration",
        existing < budget.max_positions_per_underlying * 2,  # legs, not structures
        f"{structure.underlying} has {existing} open legs vs cap "
        f"{budget.max_positions_per_underlying * 2}",
        existing_legs=existing,
    )

    # --- buying power -------------------------------------------------------
    verdict.record(
        "buying_power",
        structure.max_loss <= book.options_buying_power,
        f"needs ${structure.max_loss:,.2f}, have ${book.options_buying_power:,.2f}",
        required=round(structure.max_loss, 2),
        available=round(book.options_buying_power, 2),
    )

    verdict.admitted = all(check["passed"] for check in verdict.checks)
    if verdict.admitted:
        verdict.reasons.append("all gates passed")
    return verdict


def shrink_to_fit(
    structure: Structure, book: Book, settings: Settings, *, pending_risk: float = 0.0
) -> Structure | None:
    """Reduce quantity until the structure fits, or return None.

    Gates shrink; they never grow. If a 1-lot still breaches, we refuse.
    """
    for qty in range(structure.qty, 0, -1):
        candidate = structure.at_qty(qty)
        if evaluate(candidate, book, settings, pending_risk=pending_risk).admitted:
            return candidate
    return None
