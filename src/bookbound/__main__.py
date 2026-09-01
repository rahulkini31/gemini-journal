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

from .audit import AuditLog
from .book import load_book, missing_greeks
from .config import load_settings
from .cycle import run_cycle
from .runner import run as run_loop
from .market import Contract, market_clock, option_chain, underlying_price
from .reconcile import summarise
from .risk import evaluate
from .structures import build_vertical_debit_spreads


def _chains(settings):
    quotes: dict[str, Contract] = {}
    contracts: list[Contract] = []
    for symbol in settings.universe:
        spot = underlying_price(settings, symbol)
        if spot is None:
            print(f"  {symbol}: no price available")
            continue
        chain = option_chain(settings, symbol, spot=spot)
        print(f"  {symbol}: spot {spot:.2f}, {len(chain)} usable contracts")
        contracts.extend(chain)
        quotes.update({c.symbol: c for c in chain})
    return contracts, quotes


def cmd_status(settings) -> int:
    clock = market_clock(settings)
    print(f"market open: {clock.get('is_open')}  next open: {clock.get('next_open')}")
    print("chains:")
    _, quotes = _chains(settings)
    book = load_book(settings, quotes)
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
    contracts, quotes = _chains(settings)
    book = load_book(settings, quotes)
    unpriced = missing_greeks(book, quotes)

    built = []
    for kind in ("C", "P"):
        built.extend(build_vertical_debit_spreads(contracts, settings, kind=kind))
    print(f"\n{len(built)} structures built from {len(contracts)} contracts")

    admitted = 0
    for structure in built:
        verdict = evaluate(structure, book, settings, unpriced_positions=unpriced)
        mark = "ADMIT " if verdict.admitted else "REFUSE"
        summary = structure.summary()
        print(f"\n{mark} {summary['key']}")
        print(f"   {summary['rationale']}")
        print(f"   limit {summary['limit_price']}  max loss ${summary['max_loss']:,.0f}"
              f"  max gain ${summary['max_gain']:,.0f}  R:R {summary['reward_risk']}")
        print(f"   book delta {verdict.book_before['delta']} -> "
              f"{verdict.book_after['delta']}")
        if verdict.admitted:
            admitted += 1
        else:
            for reason in verdict.reasons:
                print(f"   x {reason}")
    print(f"\n{admitted}/{len(built)} admissible")
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
    from .book import load_book
    from .execute import close_position
    from .exits import deadline_flatten
    book = load_book(settings, {})
    orders = deadline_flatten(book.raw_positions, "operator flatten")
    if not orders:
        print("nothing open")
        return 0
    log = AuditLog(settings.audit_path)
    for order in orders:
        result = close_position(order, dry_run=not live)
        print(f"  close {order.symbol} via {order.side}: {result.status}")
        log.write("flatten", symbol=order.symbol, status=result.status,
                  ok=result.ok, dry_run=not live)
    return 0


def cmd_panic(settings) -> int:
    from .execute import cancel_all
    print(cancel_all())
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
