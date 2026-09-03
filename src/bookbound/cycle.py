"""One trading cycle with deterministic safety at every state transition."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import context as market_context
from . import execute, reconcile
from .agents import REVIEWER_SYSTEM, decide
from .audit import AuditLog
from .http import ApiError
from .book import Book, load_book, missing_greeks
from .capture import DecisionCapture
from .config import Settings
from .context import exchange_date
from .exits import ExitPlan, evaluate_structure_exits, price_exit_plan
from .market import (
    UnderlyingSnapshot,
    Contract,
    market_clock,
    option_chain,
    option_snapshots,
    parse_occ,
    quotes_are_synchronous,
    tradeable,
    underlying_snapshot,
    underlying_snapshot_is_fresh,
)
from .risk import Verdict, evaluate, shrink_to_fit
from .structures import (
    Structure,
    build_vertical_credit_spreads,
    build_vertical_debit_spreads,
    reprice_structure,
    select_diversified_shortlist,
)


@dataclass
class CycleReport:
    market_open: bool
    as_of: str | None = None
    equity: float = 0.0
    book_greeks: dict = field(default_factory=dict)
    contracts_seen: int = 0
    built_candidates: int = 0
    admissible_candidates: int = 0
    candidates: int = 0
    shortlist: list[str] = field(default_factory=list)
    exits: list = field(default_factory=list)
    repairs: list = field(default_factory=list)
    cancellations: list = field(default_factory=list)
    outcome: str = "NO_TRADE"
    detail: str = ""
    verdict: dict | None = None
    order: dict | None = None
    health: dict = field(default_factory=dict)
    capture_path: str | None = None
    capture_sha256: str | None = None
    capture_error: str | None = None


@dataclass(frozen=True)
class CandidateFunnel:
    built: tuple[Structure, ...]
    admissible: tuple[Structure, ...]
    shortlist: tuple[Structure, ...]


@dataclass(frozen=True)
class Revalidation:
    admitted: bool
    reason: str
    structure: Structure | None = None
    verdict: Verdict | None = None
    old_limit: float | None = None
    new_limit: float | None = None


def resolve_cycle_as_of(
    clock: dict,
    override: datetime | None = None,
) -> datetime:
    """Resolve one explicit, timezone-aware timestamp for a cycle phase."""
    if override is not None:
        if override.tzinfo is None or override.utcoffset() is None:
            raise ValueError("cycle as_of must be timezone-aware")
        return override.astimezone(timezone.utc)

    value = clock.get("timestamp")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("market clock timestamp is invalid") from exc
    else:
        raise ValueError("market clock timestamp is missing")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("market clock timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def construct_candidate_funnel(
    contracts: list[Contract],
    spots: dict[str, float],
    realised_vols: dict[str, float],
    book: Book,
    settings: Settings,
    *,
    unpriced_positions: list[str],
    as_of: datetime,
) -> CandidateFunnel:
    """Generate completely, gate completely, then prune/diversify to top-K."""
    budget = settings.risk
    loss_cap = book.equity * budget.max_loss_per_trade_pct
    built: list[Structure] = []
    for kind in ("C", "P"):
        built.extend(build_vertical_debit_spreads(
            contracts,
            settings,
            kind=kind,
            target_deltas=budget.debit_long_target_deltas,
            anchor_delta_tolerance=budget.anchor_delta_tolerance,
            max_candidates=None,
            spots=spots,
            max_loss_cap=loss_cap,
            realised_vols=realised_vols,
            as_of=as_of,
        ))
        built.extend(build_vertical_credit_spreads(
            contracts,
            settings,
            kind=kind,
            target_deltas=budget.credit_short_target_deltas,
            anchor_delta_tolerance=budget.anchor_delta_tolerance,
            max_candidates=None,
            spots=spots,
            max_loss_cap=loss_cap,
            realised_vols=realised_vols,
            as_of=as_of,
        ))

    admissible = [
        candidate
        for candidate in built
        if evaluate(
            candidate,
            book,
            settings,
            unpriced_positions=unpriced_positions,
        ).admitted
    ]
    shortlist = select_diversified_shortlist(
        admissible,
        budget.shortlist_size,
        max_per_underlying_expiry_direction=budget.max_shortlist_per_cluster,
        max_per_anchor=budget.max_shortlist_per_anchor,
        correlation_groups=budget.shortlist_correlation_groups,
        max_per_factor_direction=budget.max_shortlist_per_factor_direction,
        max_per_factor_direction_dte=(
            budget.max_shortlist_per_factor_direction_dte
        ),
        dte_bucket_days=budget.shortlist_dte_bucket_days,
    )
    return CandidateFunnel(tuple(built), tuple(admissible), tuple(shortlist))


def validate_refreshed_structure(
    original: Structure,
    refreshed_quotes: dict[str, Contract],
    refreshed_spot: float,
    book: Book,
    settings: Settings,
    *,
    unpriced_positions: list[str],
    as_of: datetime,
) -> Revalidation:
    """Reprice the chosen spread and rerun data, drift, and portfolio gates."""
    symbols = (original.long.symbol, original.short.symbol)
    legs = [refreshed_quotes.get(symbol) for symbol in symbols]
    if any(leg is None for leg in legs):
        return Revalidation(False, "selected leg missing from refreshed snapshot")
    complete_legs = [leg for leg in legs if leg is not None]
    if not all(
        tradeable(leg, settings, as_of=as_of) for leg in complete_legs
    ):
        return Revalidation(
            False, "refreshed quote is stale, unsized, crossed, or illiquid"
        )
    if not quotes_are_synchronous(complete_legs, settings):
        return Revalidation(False, "refreshed leg quotes are asynchronous")

    refreshed = reprice_structure(
        original,
        refreshed_quotes,
        spot=refreshed_spot,
        valuation_date=exchange_date(as_of),
    )
    if refreshed is None:
        return Revalidation(False, "refreshed quotes no longer form the structure")

    old_limit = original.limit_price()
    new_limit = refreshed.limit_price()
    deterioration = new_limit - old_limit
    allowed = max(
        0.01,
        abs(old_limit) * settings.risk.max_reprice_deterioration_pct,
    )
    if deterioration > allowed:
        return Revalidation(
            False,
            f"entry-price deterioration {deterioration:.2f} exceeds {allowed:.2f}",
            refreshed,
            old_limit=old_limit,
            new_limit=new_limit,
        )

    verdict = evaluate(
        refreshed,
        book,
        settings,
        unpriced_positions=unpriced_positions,
    )
    if not verdict.admitted:
        return Revalidation(
            False,
            "; ".join(verdict.reasons),
            refreshed,
            verdict,
            old_limit,
            new_limit,
        )
    return Revalidation(
        True, "refreshed structure passed every gate", refreshed, verdict,
        old_limit, new_limit,
    )


def _option_symbols_for_refresh(book: Book, selected: Structure) -> list[str]:
    symbols = {selected.long.symbol, selected.short.symbol}
    for position in book.raw_positions:
        symbol = str(position.get("symbol") or "")
        if position.get("asset_class") == "us_option" and parse_occ(symbol):
            symbols.add(symbol)
    for order in book.raw_orders:
        nodes = list(order.get("legs") or [])
        if not nodes and order.get("symbol"):
            nodes = [order]
        for leg in nodes:
            symbol = str(leg.get("symbol") or "")
            if parse_occ(symbol):
                symbols.add(symbol)
    return sorted(symbols)


def _result_dict(result) -> dict:
    return {
        "ok": result.ok,
        "status": result.status,
        "order_id": getattr(result, "order_id", None),
        "client_order_id": getattr(result, "client_order_id", None),
        "error": result.error,
    }


def _is_working(order: dict) -> bool:
    return str(order.get("status") or "").lower() not in {
        "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"
    }


def _working_opening_orders(orders: list[dict]) -> list[dict]:
    out = []
    for order in orders:
        if not _is_working(order):
            continue
        nodes = list(order.get("legs") or []) or [order]
        if any(
            str(node.get("position_intent") or "").endswith("_to_open")
            for node in nodes
        ):
            out.append(order)
    return out


def _whole_nonnegative(value: object) -> int | None:
    """Parse broker quantities without silently rounding fractional values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or number < 0:
        return None
    return int(number)


def _planned_close_signature(
    plan: ExitPlan,
) -> tuple[tuple[str, str, str, int], ...]:
    return tuple(sorted(
        (leg.symbol, leg.side, leg.position_intent, leg.qty)
        for leg in plan.legs
    ))


def _working_close_signature(
    order: dict,
) -> tuple[tuple[str, str, str, int], ...] | None:
    """Normalize one broker close order to its still-working leg quantities.

    Alpaca represents an MLEG's total contracts as parent ``qty`` and each
    leg's relative amount as ``ratio_qty``.  Comparing only symbols can cause a
    one-lot close to suppress a required multi-lot stop, so quantity, side and
    close intent are all part of the identity.
    """
    nodes = list(order.get("legs") or []) or [order]
    if not nodes:
        return None
    if any(
        not str(node.get("position_intent") or "").endswith("_to_close")
        for node in nodes
    ):
        return None

    parent_qty = _whole_nonnegative(order.get("qty"))
    parent_filled = _whole_nonnegative(order.get("filled_qty") or 0)
    rows: list[tuple[str, str, str, int]] = []
    for node in nodes:
        symbol = str(node.get("symbol") or "")
        side = str(node.get("side") or "")
        intent = str(node.get("position_intent") or "")
        if not symbol or not side:
            return None

        if parent_qty is not None and parent_filled is not None:
            ratio = _whole_nonnegative(node.get("ratio_qty") or 1)
            if ratio is None or ratio <= 0 or parent_filled > parent_qty:
                return None
            remaining = (parent_qty - parent_filled) * ratio
        else:
            leg_qty = _whole_nonnegative(node.get("qty"))
            leg_filled = _whole_nonnegative(node.get("filled_qty") or 0)
            if (
                leg_qty is None or leg_filled is None
                or leg_filled > leg_qty
            ):
                return None
            remaining = leg_qty - leg_filled
        if remaining <= 0:
            return None
        rows.append((symbol, side, intent, remaining))
    return tuple(sorted(rows))


def _matching_working_close(
    orders: list[dict], plan: ExitPlan,
) -> dict | None:
    target = _planned_close_signature(plan)
    for order in orders:
        if not _is_working(order):
            continue
        if _working_close_signature(order) == target:
            return order
    return None


def _price_for_close(
    plan: ExitPlan, settings: Settings, *, as_of: datetime
) -> ExitPlan | None:
    """Attach a fresh executable-side limit, or return None.

    ``build_close_payload`` refuses any plan without a quote-derived limit, so
    without this step every exit and every repair raised ExecutionError at
    submission - the agent could open positions and never close them.

    Quotes are refreshed for the exact legs immediately before submission and
    contract-master verification is skipped: that check gates ENTRIES, and a
    position that turned non-standard must still be closable.
    """
    try:
        quotes = option_snapshots(
            settings, plan.symbols, verify_contracts=False,
        )
        return price_exit_plan(plan, quotes, settings, as_of=as_of)
    except (ValueError, ApiError, OSError):
        return None


def _handle_structure_exits(
    book: Book,
    settings: Settings,
    report: CycleReport,
    audit: AuditLog,
    *,
    dry_run: bool,
    today,
    phase: str,
    as_of: datetime,
) -> bool:
    """Apply structure lifecycle rules to one freshly loaded broker state.

    Returns ``True`` when the caller must halt the cycle.  This function is
    deliberately reusable at every state transition: option-chain collection
    and model review can both be long enough for a stop or time exit to become
    actionable.
    """
    exit_plans = evaluate_structure_exits(
        book.raw_positions, settings, today=today
    )
    if not exit_plans:
        return False

    # A working parent can change the position quantity after a close has been
    # sized.  Cancel it first and wait for terminal confirmation before closing.
    live_entries = _working_opening_orders(book.raw_orders)
    if live_entries:
        for order in live_entries:
            order_id = str(order.get("id") or "")
            result = execute.cancel_order(
                order_id, dry_run=dry_run, settings=settings
            )
            row = _result_dict(result)
            report.cancellations.append(row)
            audit.write(
                "exit_parent_cancel"
                if result.ok else "exit_parent_cancel_failed",
                phase=phase,
                **row,
                dry_run=dry_run,
            )
        report.outcome = "EXIT_PENDING_CANCEL"
        report.detail = (
            "opening order canceled before structure exit; close quantity "
            "will be recalculated after terminal order confirmation"
        )
        return True

    attempted = False
    for plan in exit_plans:
        working_close = _matching_working_close(book.raw_orders, plan)
        if working_close is not None:
            report.exits.append({
                "symbols": list(plan.symbols),
                "reason": plan.reason,
                "detail": plan.detail,
                "ok": True,
                "status": "ALREADY_WORKING",
                "order_id": working_close.get("id"),
                "client_order_id": working_close.get("client_order_id"),
                "error": None,
            })
            continue
        priced = _price_for_close(plan, settings, as_of=as_of)
        if priced is None:
            # Fail closed. An exit we cannot bound is not submitted as a market
            # order; it is reported and retried next cycle.
            report.exits.append({
                "symbols": list(plan.symbols),
                "reason": plan.reason,
                "detail": plan.detail,
                "ok": False,
                "status": "UNPRICED",
                "order_id": None,
                "client_order_id": None,
                "error": "no executable quote-derived limit",
            })
            audit.write(
                "exit_unpriced", symbols=list(plan.symbols),
                reason=plan.reason, phase=phase,
            )
            attempted = True
            continue
        result = execute.close_structure(
            priced, dry_run=dry_run, settings=settings
        )
        attempted = True
        row = {
            "symbols": list(plan.symbols),
            "reason": plan.reason,
            "detail": plan.detail,
            **_result_dict(result),
        }
        report.exits.append(row)
        audit.write(
            "exit" if result.ok else "exit_failed",
            phase=phase,
            **row,
            dry_run=dry_run,
        )

    report.outcome = "EXIT_REQUIRED" if attempted else "EXIT_PENDING"
    report.detail = (
        "structure exit submitted; new entries halted"
        if attempted else "matching structure close already working"
    )
    return True


def run_cycle(
    settings: Settings,
    *,
    dry_run: bool = True,
    as_of: datetime | None = None,
) -> CycleReport:
    audit = AuditLog(settings.audit_path)

    clock = market_clock(settings)
    if not clock.get("is_open"):
        audit.write(
            "cycle_skipped", reason="market closed", next_open=clock.get("next_open")
        )
        return CycleReport(
            market_open=False, detail="market closed", outcome="MARKET_CLOSED"
        )
    try:
        cycle_as_of = resolve_cycle_as_of(clock, as_of)
    except ValueError as exc:
        audit.write("cycle_refused", reason=str(exc))
        return CycleReport(
            market_open=True, outcome="DATA_INVALID", detail=str(exc)
        )

    capture: DecisionCapture | None = None
    capture_init_error: str | None = None
    if settings.capture_root is not None:
        try:
            cycle_id = (
                f"{cycle_as_of:%Y%m%dT%H%M%S%fZ}-"
                f"{uuid.uuid4().hex[:12]}"
            )
            capture = DecisionCapture(
                settings.capture_root,
                cycle_id=cycle_id,
                as_of=cycle_as_of,
                settings=settings,
                model_provenance={
                    # Selection is deterministic and carries no prompt; only
                    # the reviewer has one, and its hash is what makes a
                    # replayed decision comparable to the original.
                    "selection": {"method": "deterministic_rank_1"},
                    "reviewer": {
                        "prompt_sha256": hashlib.sha256(
                            REVIEWER_SYSTEM.encode("utf-8")
                        ).hexdigest(),
                    },
                },
            )
        except (OSError, TypeError, ValueError) as exc:
            capture_init_error = str(exc)

    book_evidence: list[dict] = []
    decision_evidence: dict = {
        "cycle_as_of": cycle_as_of.isoformat(),
        "dry_run": dry_run,
    }

    # Load broker state before market data.  A quote-feed outage must never
    # prevent us from canceling a partial entry or repairing a naked short.
    book = load_book(settings, {}, spots={})
    health = reconcile.summarise(book.raw_positions)
    report = CycleReport(
        market_open=True,
        as_of=cycle_as_of.isoformat(),
        equity=book.equity,
        book_greeks=book.committed_greeks.as_dict(),
        health=health,
    )
    book_evidence.append({"phase": "cycle_start", "book": book})

    capture_sealed = False

    def finish(*, required_for_entry: bool = False) -> CycleReport:
        """Seal all evidence once; capture failure vetoes new risk."""
        nonlocal capture_sealed
        if capture_sealed:
            return report
        if capture is None:
            report.capture_error = capture_init_error
            if required_for_entry and settings.capture_root is not None:
                report.outcome = "CAPTURE_FAILED"
                report.detail = (
                    "immutable decision capture unavailable; entry refused"
                )
            return report
        try:
            decision_evidence["terminal_report"] = asdict(report)
            capture.record_context(decision_evidence)
            capture.record_book({"snapshots": book_evidence})
            capture.record_working_orders([
                {
                    "phase": snapshot["phase"],
                    "orders": snapshot["book"].raw_orders,
                }
                for snapshot in book_evidence
            ])
            reference = capture.seal()
            report.capture_path = str(reference.path)
            report.capture_sha256 = reference.bundle_sha256
            capture_sealed = True
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            report.capture_error = str(exc)
            audit.write("capture_failed", reason=report.capture_error)
            if required_for_entry:
                report.outcome = "CAPTURE_FAILED"
                report.detail = (
                    "immutable decision capture failed; entry refused"
                )
        return report

    # A malformed structure is repaired before any ordinary exit or entry.
    repair_plans = reconcile.build_repair_plans(book.raw_positions)
    unsupported_breaches = [
        breach for breach in health.get("breaches", [])
        if breach.get("severity") == "probable_assignment"
    ]
    if unsupported_breaches:
        report.outcome = "RECONCILIATION_UNSUPPORTED"
        report.detail = (
            "possible assignment/equity-option mismatch requires explicit "
            "operator or strategy-ledger remediation"
        )
        audit.write(
            "reconciliation_unsupported",
            reason=report.detail,
            breaches=unsupported_breaches,
        )
        return finish()
    if repair_plans:
        # A still-live parent entry can add another fill while a repair is in
        # flight.  Cancel it and wait for broker confirmation before sizing the
        # repair against a moving target.
        live_entries = _working_opening_orders(book.raw_orders)
        if live_entries:
            for order in live_entries:
                order_id = str(order.get("id") or "")
                result = execute.cancel_order(
                    order_id, dry_run=dry_run, settings=settings
                )
                row = _result_dict(result)
                report.cancellations.append(row)
                audit.write(
                    "reconciliation_parent_cancel"
                    if result.ok else "reconciliation_parent_cancel_failed",
                    **row,
                    dry_run=dry_run,
                )
            report.outcome = "RECONCILIATION_PENDING_CANCEL"
            report.detail = (
                "opening order canceled after partial fill; repair waits for "
                "confirmed terminal order state"
            )
            return finish()
        for planned in repair_plans:
            close_plan = planned.repair
            working_close = _matching_working_close(book.raw_orders, close_plan)
            if working_close is not None:
                report.repairs.append({
                    "severity": planned.breach.severity,
                    "symbols": planned.breach.symbols,
                    "action": planned.recommended_action,
                    "ok": True,
                    "status": "ALREADY_WORKING",
                    "order_id": working_close.get("id"),
                    "client_order_id": working_close.get("client_order_id"),
                    "error": None,
                })
                continue
            priced_repair = _price_for_close(
                close_plan, settings, as_of=cycle_as_of,
            )
            if priced_repair is None:
                report.repairs.append({
                    "severity": planned.breach.severity,
                    "symbols": planned.breach.symbols,
                    "action": planned.recommended_action,
                    "ok": False,
                    "status": "UNPRICED",
                    "order_id": None,
                    "client_order_id": None,
                    "error": "no executable quote-derived limit",
                })
                audit.write(
                    "reconciliation_repair_unpriced",
                    symbols=planned.breach.symbols,
                    severity=planned.breach.severity,
                )
                continue
            result = execute.close_structure(
                priced_repair, dry_run=dry_run, settings=settings
            )
            row = {
                "severity": planned.breach.severity,
                "symbols": planned.breach.symbols,
                "action": planned.recommended_action,
                **_result_dict(result),
            }
            report.repairs.append(row)
            audit.write(
                "reconciliation_repair" if result.ok else "reconciliation_repair_failed",
                **row,
                detail=planned.breach.detail,
                dry_run=dry_run,
            )
        report.outcome = "RECONCILIATION_REQUIRED"
        report.detail = "book is unbalanced; repair active and entries halted"
        return finish()

    # DAY entry orders cannot be allowed to accumulate invisibly across cycles.
    stale_orders = execute.stale_opening_orders(
        book.raw_orders,
        as_of=cycle_as_of,
        max_age_seconds=getattr(
            settings.risk, "max_working_order_age_seconds", 300.0
        ),
    )
    if stale_orders:
        for order in stale_orders:
            order_id = str(order.get("id") or "")
            result = execute.cancel_order(
                order_id, dry_run=dry_run, settings=settings
            )
            row = _result_dict(result)
            report.cancellations.append(row)
            audit.write(
                "stale_order_cancel" if result.ok else "stale_order_cancel_failed",
                **row,
                dry_run=dry_run,
            )
        report.outcome = "STALE_ORDERS"
        report.detail = "stale opening orders canceled; entries wait for next cycle"
        return finish()

    # Healthy spreads exit as one MLEG.  The same check is repeated after each
    # later broker reload because market observation and model review take time.
    if _handle_structure_exits(
        book, settings, report, audit,
        dry_run=dry_run,
        today=exchange_date(cycle_as_of),
        as_of=cycle_as_of,
        phase="cycle_start",
    ):
        return finish()

    # --- observe one coherent, timestamped market state -------------------
    quotes: dict[str, Contract] = {}
    contracts: list[Contract] = []
    spots: dict[str, float] = {}
    underlying_marks = {}
    try:
        for symbol in settings.universe:
            observed = underlying_snapshot(
                settings,
                symbol,
                raw_sink=(
                    (lambda captured_symbol, payload: capture.record_underlier_snapshot(
                        captured_symbol, payload, phase="observation"
                    ))
                    if capture is not None else None
                ),
            )
            if not underlying_snapshot_is_fresh(
                observed, settings, cycle_as_of
            ):
                continue
            assert observed is not None
            spot = observed.price
            underlying_marks[symbol] = observed
            spots[symbol] = spot
            chain = option_chain(
                settings,
                symbol,
                spot=spot,
                as_of=cycle_as_of,
                raw_sink=(capture.record_chain_page if capture is not None else None),
            )
            contracts.extend(chain)
            quotes.update({contract.symbol: contract for contract in chain})
        observation_clock = market_clock(settings)
        if not observation_clock.get("is_open"):
            raise RuntimeError("market closed while collecting option chains")
        observation_as_of = resolve_cycle_as_of(observation_clock)
        if observation_as_of < cycle_as_of:
            raise RuntimeError("broker clock moved backwards during observation")
        # RE-OBSERVE rather than age the pre-collection marks. Each mark was
        # taken before its own chain was fetched, so judging it after every
        # chain has been collected makes staleness a function of how long
        # collection took, not of data quality: with three underliers the first
        # mark had aged past the bound before any decision was possible, and
        # the whole cycle halted with DATA_INVALID on healthy data.
        #
        # The safety intent is that options are valued against a CURRENT
        # underlier, so refresh the marks and validate those.
        refreshed_marks: dict[str, UnderlyingSnapshot] = {}
        stale_underliers: list[str] = []
        for symbol in list(underlying_marks):
            try:
                observed = underlying_snapshot(settings, symbol)
            except (ApiError, OSError):
                observed = None
            if observed is None or not underlying_snapshot_is_fresh(
                observed, settings, observation_as_of
            ):
                stale_underliers.append(symbol)
                continue
            refreshed_marks[symbol] = observed
            spots[symbol] = observed.price

        # An individual underlier going quiet is a reason to ignore that
        # underlier, not to abandon the cycle: dropping it removes its
        # candidates while the rest of the book stays managed.
        if stale_underliers:
            audit.write(
                "underlier_dropped_stale",
                symbols=sorted(stale_underliers),
                phase="post_observation",
            )
            for symbol in stale_underliers:
                underlying_marks.pop(symbol, None)
                spots.pop(symbol, None)
            dropped = {s for s in stale_underliers}
            contracts = [c for c in contracts if c.underlying not in dropped]
            quotes = {
                sym: c for sym, c in quotes.items() if c.underlying not in dropped
            }
        underlying_marks.update(refreshed_marks)

        if not underlying_marks:
            raise RuntimeError(
                f"no underlier has a fresh mark after chain collection: "
                f"{sorted(stale_underliers)}"
            )
    except RuntimeError as exc:
        report.outcome = "DATA_INVALID"
        report.detail = f"market data unavailable: {exc}"
        audit.write("cycle_refused", reason=report.detail)
        return finish()

    # Re-read after data collection so fills/cancels that occurred meanwhile
    # cannot hide behind the earlier lifecycle check.
    book = load_book(settings, quotes, spots=spots)
    book_evidence.append({"phase": "post_observation", "book": book})
    health = reconcile.summarise(book.raw_positions)
    report.equity = book.equity
    report.book_greeks = book.committed_greeks.as_dict()
    report.contracts_seen = len(contracts)
    report.health = health
    if not health["healthy"]:
        report.outcome = "RECONCILIATION_CHANGED"
        report.detail = "book changed during observation; entries halted"
        audit.write("reconciliation_breach", **health)
        return finish()
    if _handle_structure_exits(
        book, settings, report, audit,
        dry_run=dry_run,
        today=exchange_date(observation_as_of),
        as_of=observation_as_of,
        phase="post_observation",
    ):
        return finish()
    unpriced = missing_greeks(book, quotes)

    # --- construct and gate before truncation -----------------------------
    context = market_context.build(settings, spots, as_of=observation_as_of)
    realised_vols = {
        symbol: node.get("realised_vol_annualised") or 0.0
        for symbol, node in (context.get("underlyings") or {}).items()
        if node.get("available")
    }
    funnel = construct_candidate_funnel(
        contracts,
        spots,
        realised_vols,
        book,
        settings,
        unpriced_positions=unpriced,
        as_of=observation_as_of,
    )
    report.built_candidates = len(funnel.built)
    report.admissible_candidates = len(funnel.admissible)
    report.candidates = len(funnel.shortlist)
    report.shortlist = [candidate.key for candidate in funnel.shortlist]
    decision_evidence.update({
        "market_context": context,
        "candidate_counts": {
            "built": len(funnel.built),
            "admissible": len(funnel.admissible),
            "shortlist": len(funnel.shortlist),
        },
        "shortlist": [candidate.summary() for candidate in funnel.shortlist],
    })
    if not funnel.shortlist:
        report.detail = (
            f"{len(funnel.built)} structures built, "
            f"{len(funnel.admissible)} passed gates, none survived shortlist policy"
        )
        audit.write(
            "no_trade",
            reason=report.detail,
            book_greeks=report.book_greeks,
            equity=book.equity,
            built=len(funnel.built),
            admissible=len(funnel.admissible),
        )
        return finish()

    llm_context = {
        "portfolio": {
            "equity": round(book.equity, 2),
            "book_greeks": report.book_greeks,
            "day_pnl_pct": round(book.day_pnl_pct, 4),
            "open_option_positions": health["option_positions"],
            "working_open_risk": round(book.pending_open_risk, 2),
        },
        **context,
    }
    decision = decide(list(funnel.shortlist), llm_context, settings)
    decision_evidence["model_decision"] = {
        "outcome": decision.outcome,
        "detail": decision.detail,
        "proposer": decision.proposer,
        "adversary": decision.adversary,
        "selected_key": (
            decision.structure.key if decision.structure is not None else None
        ),
    }
    if not decision.traded:
        report.outcome, report.detail = decision.outcome, decision.detail
        audit.write(
            "no_trade",
            reason=decision.detail,
            outcome=decision.outcome,
            proposer=decision.proposer,
            adversary=decision.adversary,
            candidates=len(funnel.shortlist),
        )
        return finish()

    sized = shrink_to_fit(
        decision.structure,
        book,
        settings,
        unpriced_positions=unpriced,
    )
    if sized is None:
        report.outcome, report.detail = "GATED", "no size fits the risk budget"
        audit.write("no_trade", reason=report.detail, key=decision.structure.key)
        return finish()

    # --- obtain a new execution snapshot after model latency --------------
    submit_clock = market_clock(settings)
    if not submit_clock.get("is_open"):
        report.outcome = "REPRICE_REJECTED"
        report.detail = "market closed before submission"
        audit.write("refused", reason=report.detail, key=sized.key)
        return finish()
    try:
        refresh_started_as_of = resolve_cycle_as_of(submit_clock)
        refreshed_underliers = {
            symbol: observed
            for symbol in settings.universe
            if (
                (observed := underlying_snapshot(
                    settings,
                    symbol,
                    raw_sink=(
                        (lambda captured_symbol, payload: capture.record_underlier_snapshot(
                            captured_symbol, payload, phase="pre_submit"
                        ))
                        if capture is not None else None
                    ),
                )) is not None
                and underlying_snapshot_is_fresh(
                    observed, settings, refresh_started_as_of
                )
            )
        }
        refreshed_spots = {
            symbol: observed.price
            for symbol, observed in refreshed_underliers.items()
        }
        selected_spot = refreshed_spots.get(sized.underlying)
        if selected_spot is None:
            raise ValueError("selected underlying spot missing at submission")
        refresh_symbols = _option_symbols_for_refresh(book, sized)
        refreshed_quotes = option_snapshots(
            settings,
            refresh_symbols,
            raw_sink=(
                capture.record_option_snapshots if capture is not None else None
            ),
        )
        submission_book = load_book(
            settings, refreshed_quotes, spots=refreshed_spots
        )
        book_evidence.append({"phase": "pre_submit", "book": submission_book})
        # Quote age must be measured after the refresh and broker reload, not
        # against the timestamp captured before those network calls began.
        final_clock = market_clock(settings)
        if not final_clock.get("is_open"):
            raise ValueError("market closed while refreshing submission state")
        submit_as_of = resolve_cycle_as_of(final_clock)
        if submit_as_of < refresh_started_as_of:
            raise ValueError("broker clock moved backwards during refresh")
        stale_underliers = sorted(
            symbol for symbol, observed in refreshed_underliers.items()
            if not underlying_snapshot_is_fresh(observed, settings, submit_as_of)
        )
        if stale_underliers:
            raise ValueError(
                f"underlying snapshots stale after refresh: {stale_underliers}"
            )
    except (RuntimeError, ValueError) as exc:
        report.outcome = "REPRICE_REJECTED"
        report.detail = str(exc)
        audit.write("refused", reason=report.detail, key=sized.key)
        return finish()

    submission_health = reconcile.summarise(submission_book.raw_positions)
    if not submission_health["healthy"]:
        report.outcome = "REPRICE_REJECTED"
        report.detail = "book became unbalanced before submission"
        report.health = submission_health
        audit.write("refused", reason=report.detail, key=sized.key)
        return finish()
    if _handle_structure_exits(
        submission_book, settings, report, audit,
        dry_run=dry_run,
        today=exchange_date(submit_as_of),
        as_of=submit_as_of,
        phase="pre_submit",
    ):
        return finish()

    submission_unpriced = missing_greeks(submission_book, refreshed_quotes)
    revalidation = validate_refreshed_structure(
        sized,
        refreshed_quotes,
        selected_spot,
        submission_book,
        settings,
        unpriced_positions=submission_unpriced,
        as_of=submit_as_of,
    )
    if revalidation.verdict is not None:
        report.verdict = {
            "admitted": revalidation.verdict.admitted,
            "book_before": revalidation.verdict.book_before,
            "book_after": revalidation.verdict.book_after,
            "checks": revalidation.verdict.checks,
            "old_limit": revalidation.old_limit,
            "new_limit": revalidation.new_limit,
        }
    if not revalidation.admitted or revalidation.structure is None:
        report.outcome, report.detail = "REPRICE_REJECTED", revalidation.reason
        audit.write(
            "refused",
            reason=revalidation.reason,
            structure=(
                revalidation.structure.summary()
                if revalidation.structure is not None else sized.summary()
            ),
            verdict=report.verdict,
        )
        return finish()

    final_structure = revalidation.structure
    decision_evidence["revalidation"] = {
        "selected": final_structure.summary(),
        "reason": revalidation.reason,
        "verdict": report.verdict,
        "submit_as_of": submit_as_of.isoformat(),
    }
    report.outcome = "ENTRY_AUTHORIZED"
    finish(required_for_entry=True)
    if report.outcome == "CAPTURE_FAILED":
        return report
    result = execute.submit(
        final_structure,
        dry_run=dry_run,
        trading_date=exchange_date(submit_as_of),
        settings=settings,
    )
    report.order = _result_dict(result)
    report.outcome = "SUBMITTED" if result.ok else "ORDER_REJECTED"
    report.detail = decision.detail
    audit.write(
        "order_submitted" if result.ok else "order_rejected",
        structure=final_structure.summary(),
        thesis=decision.proposer.get("thesis"),
        adversary=decision.adversary.get("reason"),
        book_before=(revalidation.verdict.book_before if revalidation.verdict else {}),
        book_after=(revalidation.verdict.book_after if revalidation.verdict else {}),
        order=report.order,
        cycle_as_of=cycle_as_of.isoformat(),
        submit_as_of=submit_as_of.isoformat(),
        dry_run=dry_run,
    )
    return finish()
