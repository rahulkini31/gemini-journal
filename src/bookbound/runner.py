"""The live loop.

Runs cycles on an interval during market hours and sleeps otherwise. Designed to
be left running unattended for the duration of the competition, so the failure
modes that matter are the boring ones: a transient API error must not kill the
process, and a crash must not leave the book unmanaged and unrecorded.
"""
from __future__ import annotations

import functools
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import execute
from .audit import AuditLog
from .config import Settings
from .cycle import run_cycle
from .exits import flatten_structure_plans
from .http import ApiError
from .market import market_clock

# Submissions close 2026-09-04 15:00 UTC. Flatten before then so the judged
# book is not left holding open structures through the cutoff.
# Long-running under nohup: never buffer, the operator tails this.
print = functools.partial(__builtins__["print"] if isinstance(__builtins__, dict)
                          else __builtins__.print, flush=True)  # noqa: A001

COMPETITION_DEADLINE = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)
FLATTEN_BEFORE_DEADLINE = timedelta(minutes=30)


@dataclass
class RunnerState:
    cycles: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    stopping: bool = False


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _interruptible_sleep(seconds: float, state: RunnerState, *, tick: float = 1.0) -> None:
    """Sleep in short ticks, checking the stop flag between them.

    Since PEP 475, time.sleep() RESUMES for the remaining duration after a signal
    handler returns - so a 30-second chunk makes Ctrl-C take up to 30 seconds to
    take effect. One-second ticks bound that latency at ~1s for a negligible cost.
    """
    deadline = time.monotonic() + seconds
    while not state.stopping:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(tick, remaining))


def _sleep_until_open(clock: dict, audit: AuditLog, state: RunnerState,
                      max_sleep: int) -> None:
    """Sleep until the next open, checking the stop flag as we go."""
    next_open = _parse_ts(clock.get("next_open"))
    now = datetime.now(timezone.utc)
    if next_open is None:
        seconds = 300.0
    else:
        seconds = max((next_open - now).total_seconds(), 60.0)
    audit.write("sleeping", reason="market closed",
                next_open=clock.get("next_open"),
                sleep_seconds=round(seconds))
    print(f"[{now:%H:%M:%S}] market closed, next open {clock.get('next_open')} "
          f"- sleeping {seconds / 60:.0f}m")
    _interruptible_sleep(seconds, state)


def run(
    settings: Settings,
    *,
    interval_seconds: int = 300,
    live: bool = False,
    max_cycles: int | None = None,
    max_consecutive_errors: int = 5,
) -> int:
    """Run until stopped, the deadline passes, or errors persist."""
    audit = AuditLog(settings.audit_path)
    state = RunnerState()

    def _stop(signum, _frame):
        # Do not trade from inside a signal handler; just ask the loop to finish.
        state.stopping = True
        print(f"\nsignal {signum} received - finishing the current cycle, then stopping")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    mode = "LIVE" if live else "DRY RUN"
    audit.write("runner_start", mode=mode, interval_seconds=interval_seconds,
                universe=list(settings.universe))
    print(f"bookbound runner starting - {mode}, every {interval_seconds}s")
    print(f"deadline {COMPETITION_DEADLINE:%Y-%m-%d %H:%M UTC}, "
          f"flattening {FLATTEN_BEFORE_DEADLINE} before")
    print("Ctrl-C to stop after the current cycle\n")

    flattened = False
    try:
        while not state.stopping:
            if max_cycles is not None and state.cycles >= max_cycles:
                print(f"reached max_cycles={max_cycles}")
                break

            now = datetime.now(timezone.utc)

            # --- pre-deadline flatten -----------------------------------
            if not flattened and now >= COMPETITION_DEADLINE - FLATTEN_BEFORE_DEADLINE:
                flattened = _flatten(settings, audit, live)
                if now >= COMPETITION_DEADLINE:
                    print("deadline passed - stopping")
                    break

            try:
                clock = market_clock(settings)
            except (ApiError, OSError) as exc:
                state.errors += 1
                state.consecutive_errors += 1
                audit.write("error", where="clock", error=str(exc)[:300])
                print(f"[{now:%H:%M:%S}] clock error: {str(exc)[:120]}")
                if state.consecutive_errors >= max_consecutive_errors:
                    audit.write("runner_halt", reason="consecutive clock errors")
                    print("too many consecutive errors - halting")
                    return 1
                _interruptible_sleep(min(interval_seconds, 60), state)
                continue

            if not clock.get("is_open"):
                _sleep_until_open(clock, audit, state, max_sleep=30)
                continue

            # --- one cycle ----------------------------------------------
            try:
                report = run_cycle(settings, dry_run=not live)
                state.cycles += 1
                state.consecutive_errors = 0
                _print_cycle(state.cycles, report)
            except (ApiError, OSError) as exc:
                state.errors += 1
                state.consecutive_errors += 1
                audit.write("error", where="cycle", error=str(exc)[:300])
                print(f"[{now:%H:%M:%S}] cycle error: {str(exc)[:160]}")
                if state.consecutive_errors >= max_consecutive_errors:
                    audit.write("runner_halt", reason="consecutive cycle errors",
                                errors=state.errors)
                    print("too many consecutive errors - halting")
                    return 1
            except Exception as exc:  # noqa: BLE001 - a loop that dies is worse
                state.errors += 1
                state.consecutive_errors += 1
                audit.write("error", where="cycle_unexpected",
                            error=f"{type(exc).__name__}: {exc}"[:300])
                print(f"[{now:%H:%M:%S}] UNEXPECTED {type(exc).__name__}: "
                      f"{str(exc)[:160]}")
                if state.consecutive_errors >= max_consecutive_errors:
                    audit.write("runner_halt", reason="consecutive unexpected errors")
                    return 1

            _interruptible_sleep(interval_seconds, state)
    finally:
        audit.write("runner_stop", cycles=state.cycles, errors=state.errors)
        print(f"\nstopped after {state.cycles} cycles, {state.errors} errors")
        print(f"decision log: {settings.audit_path}")
    return 0


def _flatten(settings: Settings, audit: AuditLog, live: bool) -> bool:
    """Close everything ahead of the submission deadline."""
    from .book import load_book
    print("\n=== pre-deadline flatten ===")
    try:
        book = load_book(settings, {})
        plans = flatten_structure_plans(
            book.raw_positions, "pre-deadline flatten"
        )
        if not plans:
            audit.write("flatten", result="nothing open")
            print("nothing open")
            return True
        for plan in plans:
            result = execute.close_structure(
                plan, dry_run=not live, settings=settings
            )
            audit.write(
                "flatten", symbols=list(plan.symbols), status=result.status,
                ok=result.ok, error=result.error, dry_run=not live,
            )
            print(f"  close {','.join(plan.symbols)}: {result.status}")
        if live:
            execute.cancel_all(settings)
        return True
    except Exception as exc:  # noqa: BLE001
        audit.write("error", where="flatten", error=str(exc)[:300])
        print(f"flatten failed: {str(exc)[:200]}")
        return False


def _print_cycle(number: int, report) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    greeks = report.book_greeks or {}
    print(
        f"[{stamp}] cycle {number:>3}  {report.outcome:<14} "
        f"eq ${report.equity:,.0f}  "
        f"delta {greeks.get('delta', 0):>7.1f}  vega {greeks.get('vega', 0):>7.1f}  "
        f"theta {greeks.get('theta', 0):>7.1f}  "
        f"cand {report.candidates:>2}"
    )
    for exit_record in report.exits or []:
        symbols = exit_record.get("symbols") or [exit_record.get("symbol", "unknown")]
        print(f"            exit {','.join(symbols)} "
              f"({exit_record['reason']}: {exit_record['detail']})")
    if report.detail:
        print(f"            {report.detail[:150]}")
    if not (report.health or {}).get("healthy", True):
        print(f"            !! {report.health}")
