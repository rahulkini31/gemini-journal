"""Replayable scheduled-event and American-option assignment controls.

The deterministic layer consumes a versioned calendar snapshot supplied by the
caller.  It never consults wall-clock time or a network service implicitly, so
the same captured inputs produce the same gate result offline.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .context import exchange_date
from .structures import Structure


@dataclass(frozen=True)
class MacroEvent:
    """A scheduled release whose jump risk affects zero or more underlyings.

    An empty ``underlyings`` tuple means a market-wide event.
    """

    name: str
    starts_at: datetime
    underlyings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DividendEvent:
    """A declared cash distribution used for short-call assignment checks."""

    underlying: str
    ex_date: date
    cash_amount: float


@dataclass(frozen=True)
class EventCalendar:
    """Finite, source-labelled calendar coverage captured with a decision."""

    source: str
    coverage_start: date
    coverage_end: date
    macro_events: tuple[MacroEvent, ...] = ()
    dividends: tuple[DividendEvent, ...] = ()

    def covers(self, value: date | datetime) -> bool:
        observed = exchange_date(value)
        return self.coverage_start <= observed <= self.coverage_end

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "coverage": {
                "start": self.coverage_start.isoformat(),
                "end": self.coverage_end.isoformat(),
            },
            "macro_events": [
                {
                    "name": event.name,
                    "starts_at": _aware_utc(event.starts_at).isoformat(),
                    "underlyings": list(event.underlyings),
                }
                for event in self.macro_events
            ],
            "dividends": [
                {
                    "underlying": event.underlying,
                    "ex_date": event.ex_date.isoformat(),
                    "cash_amount": event.cash_amount,
                }
                for event in self.dividends
            ],
        }


@dataclass(frozen=True)
class EventRiskAssessment:
    """Forecast-free veto plus the evidence exposed to audit/model layers."""

    allowed: bool
    reasons: tuple[str, ...] = ()
    annotations: dict[str, Any] = field(default_factory=dict)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("macro event timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not valid ISO-8601") from exc
    return _aware_utc(parsed)


def _parse_date(value: Any, *, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO date") from exc


def load_event_calendar(path: Path | str | None) -> EventCalendar | None:
    """Load one strict JSON calendar snapshot; missing configuration is explicit."""
    if path is None:
        return None
    source_path = Path(path)
    if not source_path.exists():
        return None
    payload = json.loads(source_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("event calendar root must be an object")
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("event calendar requires coverage.start and coverage.end")
    source = str(payload.get("source") or "").strip()
    if not source:
        raise ValueError("event calendar source is required")
    start = _parse_date(coverage.get("start"), field_name="coverage.start")
    end = _parse_date(coverage.get("end"), field_name="coverage.end")
    if end < start:
        raise ValueError("event calendar coverage.end precedes coverage.start")

    macro: list[MacroEvent] = []
    for index, raw in enumerate(payload.get("macro_events") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"macro_events[{index}] must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"macro_events[{index}].name is required")
        underlyings = tuple(
            sorted({str(symbol).strip().upper() for symbol in (raw.get("underlyings") or []) if str(symbol).strip()})
        )
        macro.append(MacroEvent(
            name=name,
            starts_at=_parse_datetime(
                raw.get("starts_at"), field_name=f"macro_events[{index}].starts_at"
            ),
            underlyings=underlyings,
        ))

    dividends: list[DividendEvent] = []
    for index, raw in enumerate(payload.get("dividends") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"dividends[{index}] must be an object")
        underlying = str(raw.get("underlying") or "").strip().upper()
        if not underlying:
            raise ValueError(f"dividends[{index}].underlying is required")
        try:
            amount = float(raw.get("cash_amount"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"dividends[{index}].cash_amount must be numeric") from exc
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError(f"dividends[{index}].cash_amount must be positive")
        dividends.append(DividendEvent(
            underlying=underlying,
            ex_date=_parse_date(
                raw.get("ex_date"), field_name=f"dividends[{index}].ex_date"
            ),
            cash_amount=amount,
        ))

    return EventCalendar(
        source=source,
        coverage_start=start,
        coverage_end=end,
        macro_events=tuple(sorted(macro, key=lambda event: (event.starts_at, event.name))),
        dividends=tuple(sorted(dividends, key=lambda event: (event.ex_date, event.underlying))),
    )


def _as_aware_datetime(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def assess_structure_event_risk(
    structure: Structure,
    *,
    spot: float,
    as_of: date | datetime | None,
    calendar: EventCalendar | None,
    settings: Settings,
) -> EventRiskAssessment:
    """Veto imminent jump risk and economically exercisable short options.

    This is deliberately a rules engine, not an event-direction forecast.  It
    asks whether a new position would cross a configured uncertainty window or
    whether the American short leg has too little time value to protect it from
    rational exercise.
    """
    budget = settings.risk
    observed_at = _as_aware_datetime(as_of)
    observed_date = exchange_date(observed_at) if observed_at is not None else None
    calendar_available = (
        calendar is not None
        and observed_date is not None
        and calendar.covers(observed_date)
    )
    reasons: list[str] = []
    annotations: dict[str, Any] = {
        "calendar_source": calendar.source if calendar is not None else "unavailable",
        "calendar_coverage_available": calendar_available,
        "macro_events_in_window": [],
        "short_extrinsic": None,
        "short_put_carry_benefit": None,
    }

    if getattr(budget, "require_event_calendar", False) and not calendar_available:
        reasons.append("calendar coverage unavailable")

    if calendar_available and calendar is not None and observed_at is not None:
        before_hours = float(getattr(budget, "macro_entry_blackout_hours", 24.0))
        after_minutes = float(getattr(budget, "macro_post_event_blackout_minutes", 30.0))
        for event in calendar.macro_events:
            affects = not event.underlyings or structure.underlying in event.underlyings
            hours_until = (_aware_utc(event.starts_at) - observed_at).total_seconds() / 3600.0
            if affects and -(after_minutes / 60.0) <= hours_until <= before_hours:
                annotations["macro_events_in_window"].append({
                    "name": event.name,
                    "starts_at": _aware_utc(event.starts_at).isoformat(),
                    "hours_until": round(hours_until, 4),
                })
                reasons.append(
                    f"scheduled event '{event.name}' is {hours_until:.2f} hours away"
                )

    valid_spot = isinstance(spot, (int, float)) and math.isfinite(float(spot)) and spot > 0
    if valid_spot and observed_date is not None:
        short = structure.short
        intrinsic = (
            max(float(spot) - short.strike, 0.0)
            if short.kind.upper() == "C"
            else max(short.strike - float(spot), 0.0)
        )
        extrinsic = max(short.mid - intrinsic, 0.0)
        annotations["short_extrinsic"] = round(extrinsic, 6)
        buffer = float(getattr(budget, "assignment_extrinsic_buffer", 0.05))

        if short.kind.upper() == "C" and intrinsic > 0 and calendar_available and calendar:
            lookahead = int(getattr(budget, "ex_dividend_lookahead_days", 5))
            for dividend in calendar.dividends:
                days_until = (dividend.ex_date - observed_date).days
                if (
                    dividend.underlying == structure.underlying
                    and 0 <= days_until <= lookahead
                    and dividend.ex_date <= structure.expiry
                    and extrinsic <= dividend.cash_amount + buffer
                ):
                    annotations["ex_dividend"] = {
                        "date": dividend.ex_date.isoformat(),
                        "cash_amount": dividend.cash_amount,
                        "days_until": days_until,
                    }
                    reasons.append(
                        f"ITM short call has ${extrinsic:.3f} extrinsic before "
                        f"{dividend.ex_date.isoformat()} ex-dividend ${dividend.cash_amount:.3f}"
                    )
                    break

        if short.kind.upper() == "P" and intrinsic > 0:
            days_to_expiry = max((structure.expiry - observed_date).days, 0)
            annual_rate = max(float(getattr(budget, "short_put_carry_rate", 0.05)), 0.0)
            carry = short.strike * math.expm1(annual_rate * days_to_expiry / 365.0)
            annotations["short_put_carry_benefit"] = round(carry, 6)
            if extrinsic <= carry + buffer:
                reasons.append(
                    f"ITM short put has ${extrinsic:.3f} extrinsic vs "
                    f"${carry:.3f} short-put carry benefit"
                )

    return EventRiskAssessment(
        allowed=not reasons,
        reasons=tuple(reasons),
        annotations=annotations,
    )
