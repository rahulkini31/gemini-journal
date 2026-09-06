"""Deterministic risk gates. No LLM is ever consulted here.

Gates may VETO or SHRINK a candidate. They can never enlarge one, invent one,
or approve something the strategy layer did not propose.

Every rejection carries the bound that failed and the numbers on both sides of
it, because the refusal log is a deliverable, not a byproduct.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from .book import (
    Book,
    Greeks,
    dollar_delta_by_underlying,
    greek_stress_loss,
    greeks_of_legs,
    option_portfolio_remaining_loss,
)
from .config import Settings
from .events import EventCalendar, assess_structure_event_risk
from .market import parse_occ
from .structures import (
    Structure,
    payoff_shape_metrics,
    payoff_shape_violations,
    submitted_execution_cost,
    terminal_expected_pnl,
)


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
    pending_risk: float | None = None,
    as_of: date | datetime | None = None,
    event_calendar: EventCalendar | None = None,
) -> Verdict:
    """Admit or refuse a candidate structure based on the POST-TRADE book.

    ``pending_risk`` carries max-loss already committed earlier in this cycle but
    not yet reflected in ``book.raw_positions``. Without it, admitting several
    structures in one pass would each measure risk against a stale book.
    """
    budget = settings.risk
    verdict = Verdict(admitted=False)

    # Working opening orders are commitments: they may fill after this decision.
    before = book.committed_greeks
    after_book = book.with_added(structure.legs)
    after = after_book.committed_greeks
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

    candidate_symbols = {leg.symbol for leg in structure.legs}
    duplicate_order_ids = []
    for order in book.raw_orders:
        status = str(order.get("status") or "").lower()
        if status in {"filled", "canceled", "cancelled", "expired", "rejected"}:
            continue
        opening_symbols = {
            str(node.get("symbol") or "")
            for node in (order.get("legs") or [])
            if str(node.get("position_intent") or "").endswith("_to_open")
        }
        if opening_symbols == candidate_symbols:
            duplicate_order_ids.append(str(order.get("id") or order.get("client_order_id") or "unknown"))
    verdict.record(
        "duplicate_working_order",
        not duplicate_order_ids,
        (
            "no working order has the same legs"
            if not duplicate_order_ids
            else f"same legs already working in {duplicate_order_ids}"
        ),
        duplicates=len(duplicate_order_ids),
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

    shape = payoff_shape_metrics(structure)
    shape_violations = payoff_shape_violations(structure, settings)
    verdict.record(
        "payoff_shape",
        not shape_violations,
        (
            "payoff geometry meets deterministic floors"
            if not shape_violations else "; ".join(shape_violations)
        ),
        **{name: round(value, 5) for name, value in shape.items()},
    )

    event_assessment = assess_structure_event_risk(
        structure,
        spot=float(book.spots.get(structure.underlying) or structure.spot or 0.0),
        as_of=as_of or structure.valuation_date,
        calendar=event_calendar,
        settings=settings,
    )
    verdict.record(
        "event_assignment_risk",
        event_assessment.allowed,
        (
            "no configured event or early-assignment veto"
            if event_assessment.allowed
            else "; ".join(event_assessment.reasons)
        ),
    )
    verdict.checks[-1]["annotations"] = event_assessment.annotations

    # --- aggregate open risk ------------------------------------------------
    open_risk = option_portfolio_remaining_loss(book.raw_positions)
    reserved = book.pending_open_risk if pending_risk is None else pending_risk
    open_risk += reserved
    total_cap = book.equity * budget.max_total_open_risk_pct
    verdict.record(
        "total_open_risk",
        open_risk + structure.max_loss <= total_cap,
        f"open risk ${open_risk:,.2f} (incl. ${reserved:,.2f} pending) "
        f"+ ${structure.max_loss:,.2f} vs cap ${total_cap:,.2f}",
        open_risk=round(open_risk, 2),
        pending=round(reserved, 2),
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

    # Raw share delta can cancel across differently priced ETFs while leaving a
    # substantial economic exposure.  Keep the legacy net budget, and also gate
    # gross share delta plus per-underlying dollar delta for a 1% move.
    committed_legs = [*book.legs, *book.pending_legs, *structure.legs]
    gross_delta = sum(
        abs(leg.delta * leg.qty * leg.multiplier) for leg in committed_legs
    )
    gross_delta_cap = getattr(budget, "max_gross_delta", budget.max_abs_delta * 10)
    verdict.record(
        "gross_delta",
        gross_delta <= gross_delta_cap,
        f"gross delta {gross_delta:,.1f} vs budget {gross_delta_cap:,.1f}",
        gross=round(gross_delta, 2), budget=gross_delta_cap,
    )
    dollar_delta = dollar_delta_by_underlying(
        committed_legs, book.spots, move_pct=0.01
    )
    dollar_delta_cap = book.equity * getattr(
        budget, "max_dollar_delta_1pct_equity_pct", 0.02
    )
    worst_dollar_delta = max((abs(value) for value in dollar_delta.values()), default=0.0)
    required_underlyings = set()
    for leg in committed_legs:
        parsed = parse_occ(leg.symbol)
        required_underlyings.add(leg.underlying or (parsed[0] if parsed else leg.symbol))
    missing_spots = sorted(
        underlying
        for underlying in required_underlyings
        if not (
            isinstance(book.spots.get(underlying), (int, float))
            and math.isfinite(float(book.spots[underlying]))
            and float(book.spots[underlying]) > 0
        )
    )
    dollar_known = not missing_spots
    verdict.record(
        "dollar_delta_by_underlying",
        dollar_known and worst_dollar_delta <= dollar_delta_cap,
        (
            f"worst 1% underlying move ${worst_dollar_delta:,.2f} vs cap "
            f"${dollar_delta_cap:,.2f}"
            if dollar_known else f"missing valid spots for {missing_spots}"
        ),
        worst=round(worst_dollar_delta, 2), cap=round(dollar_delta_cap, 2),
    )

    stress = greek_stress_loss(committed_legs, book.spots) if dollar_known else 0.0
    stress_cap = book.equity * getattr(budget, "max_local_stress_loss_pct", 0.03)
    verdict.record(
        "correlated_greek_stress",
        dollar_known and stress <= stress_cap,
        (
            f"worst local spot/vol scenario loss ${stress:,.2f} vs cap ${stress_cap:,.2f}"
            if dollar_known else "spot marks unavailable; stress gate not evaluated"
        ),
        loss=round(stress, 2), cap=round(stress_cap, 2),
    )

    # --- expectancy ---------------------------------------------------------
    # Probability of profit alone is not a quality measure: it rewards exactly
    # the trade that collects a little very often and gives it all back at once.
    # The rank-1 candidate under the old score had PoP 0.795 with $151 of upside
    # against $949 of downside - a 79.5% chance of $151 and a 20.5% chance of
    # -$949 is NEGATIVE expectancy, and it was being ranked first.
    #
    # This diagnostic is model-based and inherits the terminal scenario's skew
    # and policy caveats. Until it is calibrated and policy-aware, making it
    # mandatory would turn an arbitrary proxy into an entry veto.
    expectancy = terminal_expected_pnl(structure)
    expectancy_required = getattr(budget, "require_positive_expectancy", False)
    scenario_eligible = (
        expectancy is not None
        and structure.scenario_calibrated
        and structure.scenario_policy_aware
    )
    expectancy_passed = (
        not expectancy_required
        or (scenario_eligible and expectancy is not None and expectancy > 0)
    )
    if expectancy is None:
        expectancy_detail = "terminal-expiry scenario unavailable"
    else:
        expectancy_detail = (
            f"terminal-expiry scenario expected P&L ${expectancy:,.2f}; "
            f"measure=physical_proxy_lognormal, "
            f"calibrated={structure.scenario_calibrated}, "
            f"policy_aware={structure.scenario_policy_aware}"
        )
    verdict.record(
        "positive_expectancy",
        expectancy_passed,
        f"{expectancy_detail}; "
        f"{'required' if expectancy_required else 'diagnostic only'}",
        terminal_scenario_expected_pnl=(
            round(expectancy, 2) if expectancy is not None else None
        ),
        measure="physical_proxy_lognormal",
        horizon="expiry",
        calibrated=structure.scenario_calibrated,
        policy_aware=structure.scenario_policy_aware,
        required=expectancy_required,
    )

    # --- edge erosion -------------------------------------------------------
    # Crossing a wide indicative quote consumes the finite risk budget even
    # without making any forecast claim. Refuse excessive observed friction.
    drag = submitted_execution_cost(structure)
    drag_cap = structure.max_loss * budget.max_edge_erosion_pct
    verdict.record(
        "edge_erosion",
        drag <= drag_cap,
        f"observed execution cost ${drag:,.2f} vs cap ${drag_cap:,.2f} "
        f"({budget.max_edge_erosion_pct:.0%} of max loss)",
        drag=round(drag, 2), cap=round(drag_cap, 2),
    )

    # --- concentration ------------------------------------------------------
    counts = book.positions_by_underlying()
    existing = counts.get(structure.underlying, 0)
    pending_units = sum(
        abs(leg.qty)
        for leg in book.pending_legs
        if (getattr(leg, "symbol", "").startswith(structure.underlying))
    )
    candidate_units = sum(abs(leg.qty) for leg in structure.legs)
    after_units = existing + pending_units + candidate_units
    unit_cap = budget.max_positions_per_underlying * 2
    verdict.record(
        "concentration",
        after_units <= unit_cap,
        f"{structure.underlying} contract units {existing} held + "
        f"{pending_units} pending + {candidate_units} candidate = {after_units} "
        f"vs cap {unit_cap}",
        existing_contract_units=existing,
        pending_contract_units=pending_units,
        candidate_contract_units=candidate_units,
        after_contract_units=after_units,
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
    structure: Structure,
    book: Book,
    settings: Settings,
    *,
    pending_risk: float | None = None,
    unpriced_positions: list[str] | None = None,
) -> Structure | None:
    """Reduce quantity until the structure fits, or return None.

    Gates shrink; they never grow. If a 1-lot still breaches, we refuse.
    """
    for qty in range(structure.qty, 0, -1):
        candidate = structure.at_qty(qty)
        if evaluate(
            candidate,
            book,
            settings,
            pending_risk=pending_risk,
            unpriced_positions=unpriced_positions,
        ).admitted:
            return candidate
    return None
