"""CLI entry point.

    python -m bookbound status      account, book greeks, position health
    python -m bookbound scan        build and gate candidates, no model, no orders
    python -m bookbound cycle       one full cycle, DRY RUN by default
    python -m bookbound cycle --live    actually submit
    python -m bookbound run         live loop: cycles on a schedule
    python -m bookbound run --live --interval 300
    python -m bookbound flatten     close every open option position
    python -m bookbound audit       decision log summary
    python -m bookbound panic       cancel every open order
"""
from __future__ import annotations

import json
import sys

from . import context as market_context
from . import execute
from .audit import AuditLog
from .book import load_book, missing_greeks
from .config import load_settings
from .cycle import construct_candidate_funnel, resolve_cycle_as_of, run_cycle
from .runner import run as run_loop
from .market import Contract, market_clock, option_chain, underlying_price
from .reconcile import summarise
from .risk import evaluate
from .exits import flatten_structure_plans


def _chains(settings, *, as_of=None):
    quotes: dict[str, Contract] = {}
    contracts: list[Contract] = []
    spots: dict[str, float] = {}
    for symbol in settings.universe:
        spot = underlying_price(
            settings,
            symbol,
            as_of=as_of,
            require_fresh=as_of is not None,
        )
        if spot is None:
            print(f"  {symbol}: no price available")
            continue
        spots[symbol] = spot
        chain = option_chain(settings, symbol, spot=spot, as_of=as_of)
        print(f"  {symbol}: spot {spot:.2f}, {len(chain)} usable contracts")
        contracts.extend(chain)
        quotes.update({c.symbol: c for c in chain})
    return contracts, quotes, spots


def cmd_status(settings) -> int:
    clock = market_clock(settings)
    as_of = resolve_cycle_as_of(clock)
    print(f"market open: {clock.get('is_open')}  next open: {clock.get('next_open')}")
    print("chains:")
    _, quotes, spots = _chains(settings, as_of=as_of)
    book = load_book(settings, quotes, spots=spots)
    print(f"\nequity ${book.equity:,.2f}   day P&L {book.day_pnl_pct:+.2%}")
    print(f"options buying power ${book.options_buying_power:,.2f}")
    print(f"book greeks: {book.greeks.as_dict()}")
    unpriced = missing_greeks(book, quotes)
    if unpriced:
        print(f"UNPRICED positions (risk engine will fail closed): {unpriced}")
    health = summarise(book.raw_positions)
    print(f"position health: {'OK' if health['healthy'] else health}")
    return 0


def cmd_scan(settings) -> int:
    """Deterministic path only - no model is consulted, no order is placed."""
    as_of = resolve_cycle_as_of(market_clock(settings))
    contracts, quotes, spots = _chains(settings, as_of=as_of)
    book = load_book(settings, quotes, spots=spots)
    unpriced = missing_greeks(book, quotes)
    context = market_context.build(settings, spots, as_of=as_of)
    realised_vols = {
        symbol: node.get("realised_vol_annualised") or 0.0
        for symbol, node in (context.get("underlyings") or {}).items()
        if node.get("available")
    }
    funnel = construct_candidate_funnel(
        contracts, spots, realised_vols, book, settings,
        unpriced_positions=unpriced, as_of=as_of,
    )
    print(
        f"\n{len(funnel.built)} structures built from {len(contracts)} contracts; "
        f"{len(funnel.admissible)} passed gates; {len(funnel.shortlist)} shortlisted"
    )

    admitted_keys = {candidate.key for candidate in funnel.admissible}
    shortlist_keys = {candidate.key for candidate in funnel.shortlist}
    for structure in funnel.built:
        verdict = evaluate(structure, book, settings, unpriced_positions=unpriced)
        mark = "SHORT " if structure.key in shortlist_keys else (
            "ADMIT " if structure.key in admitted_keys else "REFUSE"
        )
        summary = structure.summary()
        print(f"\n{mark} {summary['key']}")
        print(f"   {summary['rationale']}")
        print(f"   limit {summary['limit_price']}  max loss ${summary['max_loss']:,.0f}"
              f"  max gain ${summary['max_gain']:,.0f}  R:R {summary['reward_risk']}")
        print(f"   book delta {verdict.book_before['delta']} -> "
              f"{verdict.book_after['delta']}")
        if not verdict.admitted:
            for reason in verdict.reasons:
                print(f"   x {reason}")
    print(f"\n{len(funnel.admissible)}/{len(funnel.built)} admissible")
    return 0


def cmd_cycle(settings, live: bool) -> int:
    report = run_cycle(settings, dry_run=not live)
    print(json.dumps(report.__dict__, indent=2, default=str))
    return 0


def cmd_audit(settings) -> int:
    log = AuditLog(settings.audit_path)
    records = log.read_all()
    print(f"{len(records)} decisions logged at {log.path}")
    for event, count in sorted(log.counts().items()):
        print(f"  {event:24} {count}")
    for record in records[-5:]:
        print(f"\n{record['ts']}  {record['event']}")
        print(f"  {json.dumps({k: v for k, v in record.items() if k not in ('ts', 'event')}, default=str)[:400]}")
    return 0


def cmd_run(settings, argv: list[str]) -> int:
    interval = 300
    if "--interval" in argv:
        interval = int(argv[argv.index("--interval") + 1])
    max_cycles = None
    if "--max-cycles" in argv:
        max_cycles = int(argv[argv.index("--max-cycles") + 1])
    live = "--live" in argv
    if live:
        print("*** LIVE MODE - orders will be placed on the paper account ***")
    return run_loop(settings, interval_seconds=interval, live=live,
                    max_cycles=max_cycles)


def cmd_flatten(settings, live: bool) -> int:
    book = load_book(settings, {})
    plans = flatten_structure_plans(book.raw_positions, "operator flatten")
    if not plans:
        print("nothing open")
        return 0
    log = AuditLog(settings.audit_path)
    for plan in plans:
        result = execute.close_structure(
            plan, dry_run=not live, settings=settings
        )
        print(f"  close {','.join(plan.symbols)} atomically: {result.status}")
        log.write(
            "flatten", symbols=list(plan.symbols), status=result.status,
            ok=result.ok, dry_run=not live,
        )
    return 0


def cmd_panic(settings) -> int:
    print(execute.cancel_all(settings))
    AuditLog(settings.audit_path).write("panic", reason="operator invoked kill switch")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    settings = load_settings()
    if command == "status":
        return cmd_status(settings)
    if command == "scan":
        return cmd_scan(settings)
    if command == "cycle":
        return cmd_cycle(settings, live="--live" in argv)
    if command == "run":
        return cmd_run(settings, argv)
    if command == "flatten":
        return cmd_flatten(settings, live="--live" in argv)
    if command == "audit":
        return cmd_audit(settings)
    if command == "panic":
        return cmd_panic(settings)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
