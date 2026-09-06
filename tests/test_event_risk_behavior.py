"""Behavior scenarios for scheduled-event and early-assignment controls."""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.book import Book  # noqa: E402
from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.events import (  # noqa: E402
    DividendEvent,
    EventCalendar,
    MacroEvent,
    assess_structure_event_risk,
    load_event_calendar,
)
from bookbound.market import Contract  # noqa: E402
from bookbound.risk import evaluate  # noqa: E402
from bookbound.structures import Structure  # noqa: E402


AS_OF = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
EXPIRY = date(2026, 9, 18)
SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())


def option(
    symbol: str,
    *,
    strike: float,
    kind: str,
    bid: float,
    ask: float,
    delta: float,
) -> Contract:
    return Contract(
        symbol=symbol,
        underlying="SPY",
        expiry=EXPIRY,
        kind=kind,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
        gamma=0.01,
        theta=-0.03,
        vega=0.08,
        iv=0.20,
    )


def bear_call(*, short_bid: float = 1.20, short_ask: float = 1.25) -> Structure:
    long = option(
        "SPY260918C00105000", strike=105, kind="C",
        bid=0.08, ask=0.10, delta=0.10,
    )
    short = option(
        "SPY260918C00100000", strike=100, kind="C",
        bid=short_bid, ask=short_ask, delta=0.30,
    )
    credit = short_bid - long.ask
    return Structure(
        name="bear_call_credit",
        underlying="SPY",
        expiry=EXPIRY,
        qty=1,
        long=long,
        short=short,
        net_debit=-credit,
        max_loss=(5 - credit) * 100,
        max_gain=credit * 100,
        spot=101.0,
        realised_vol=0.20,
        forward=101.0,
        valuation_date=AS_OF.date(),
    )


def bull_put(*, short_bid: float = 5.05, short_ask: float = 5.10) -> Structure:
    long = option(
        "SPY260918P00100000", strike=100, kind="P",
        bid=0.03, ask=0.05, delta=-0.10,
    )
    short = option(
        "SPY260918P00105000", strike=105, kind="P",
        bid=short_bid, ask=short_ask, delta=-0.30,
    )
    credit = short_bid - long.ask
    return Structure(
        name="bull_put_credit",
        underlying="SPY",
        expiry=EXPIRY,
        qty=1,
        long=long,
        short=short,
        net_debit=-credit,
        max_loss=max(0.0, (5 - credit) * 100),
        max_gain=credit * 100,
        spot=100.0,
        realised_vol=0.20,
        forward=100.0,
        valuation_date=AS_OF.date(),
    )


def calendar(*, macro=(), dividends=()) -> EventCalendar:
    return EventCalendar(
        source="test-fixture",
        coverage_start=date(2026, 9, 1),
        coverage_end=date(2026, 10, 1),
        macro_events=tuple(macro),
        dividends=tuple(dividends),
    )


class TestScheduledEventBlackout(unittest.TestCase):
    def test_given_imminent_macro_release_then_new_structure_is_blocked(self):
        events = calendar(macro=(MacroEvent(
            name="US employment report",
            starts_at=datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc),
            underlyings=("SPY", "QQQ", "IWM"),
        ),))

        result = assess_structure_event_risk(
            bear_call(), spot=101.0, as_of=AS_OF,
            calendar=events, settings=SETTINGS,
        )

        self.assertFalse(result.allowed)
        self.assertTrue(any("employment" in reason for reason in result.reasons))
        self.assertEqual(result.annotations["calendar_source"], "test-fixture")

    def test_given_event_for_another_underlying_then_it_does_not_block(self):
        events = calendar(macro=(MacroEvent(
            name="single-name earnings",
            starts_at=datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc),
            underlyings=("AAPL",),
        ),))

        result = assess_structure_event_risk(
            bear_call(), spot=101.0, as_of=AS_OF,
            calendar=events, settings=SETTINGS,
        )

        self.assertTrue(result.allowed, result.reasons)


class TestEarlyAssignmentEconomics(unittest.TestCase):
    def test_given_itm_short_call_before_ex_div_then_thin_extrinsic_is_blocked(self):
        events = calendar(dividends=(DividendEvent(
            underlying="SPY", ex_date=date(2026, 9, 4), cash_amount=1.00,
        ),))

        result = assess_structure_event_risk(
            bear_call(), spot=101.0, as_of=AS_OF,
            calendar=events, settings=SETTINGS,
        )

        self.assertFalse(result.allowed)
        self.assertTrue(any("ex-dividend" in reason for reason in result.reasons))
        self.assertAlmostEqual(result.annotations["short_extrinsic"], 0.225)

    def test_given_itm_short_call_with_extrinsic_above_dividend_then_it_is_allowed(self):
        events = calendar(dividends=(DividendEvent(
            underlying="SPY", ex_date=date(2026, 9, 4), cash_amount=1.00,
        ),))

        result = assess_structure_event_risk(
            bear_call(short_bid=2.25, short_ask=2.35),
            spot=101.0,
            as_of=AS_OF,
            calendar=events,
            settings=SETTINGS,
        )

        self.assertTrue(result.allowed, result.reasons)

    def test_given_deep_itm_short_put_then_carry_can_make_assignment_economic(self):
        result = assess_structure_event_risk(
            bull_put(), spot=100.0, as_of=AS_OF,
            calendar=calendar(), settings=SETTINGS,
        )

        self.assertFalse(result.allowed)
        self.assertTrue(any("short-put carry" in reason for reason in result.reasons))
        self.assertGreater(result.annotations["short_put_carry_benefit"], 0.05)


class TestEventCalendarProvenance(unittest.TestCase):
    def test_given_required_but_unavailable_calendar_then_gate_fails_closed(self):
        strict = dataclasses.replace(
            SETTINGS,
            risk=dataclasses.replace(SETTINGS.risk, require_event_calendar=True),
        )

        result = assess_structure_event_risk(
            bear_call(), spot=101.0, as_of=AS_OF,
            calendar=None, settings=strict,
        )

        self.assertFalse(result.allowed)
        self.assertIn("calendar coverage unavailable", result.reasons)

    def test_given_json_calendar_then_timezone_and_coverage_are_replayable(self):
        payload = {
            "source": "controlled-test-feed",
            "coverage": {"start": "2026-09-01", "end": "2026-10-01"},
            "macro_events": [{
                "name": "FOMC decision",
                "starts_at": "2026-09-16T18:00:00Z",
                "underlyings": ["SPY", "QQQ", "IWM"],
            }],
            "dividends": [{
                "underlying": "SPY", "ex_date": "2026-09-18", "cash_amount": 1.23,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            path.write_text(json.dumps(payload))
            loaded = load_event_calendar(path)

        self.assertEqual(loaded.source, "controlled-test-feed")
        self.assertEqual(loaded.macro_events[0].starts_at.tzinfo, timezone.utc)
        self.assertEqual(loaded.dividends[0].cash_amount, 1.23)
        self.assertTrue(loaded.covers(date(2026, 9, 2)))

    def test_given_macro_event_then_deterministic_risk_stack_records_the_veto(self):
        events = calendar(macro=(MacroEvent(
            name="CPI release",
            starts_at=datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc),
            underlyings=("SPY",),
        ),))
        account = Book(
            equity=100_000,
            last_equity=100_000,
            cash=100_000,
            buying_power=400_000,
            options_buying_power=100_000,
            legs=[],
            raw_positions=[],
            spots={"SPY": 101.0},
        )

        verdict = evaluate(
            bear_call(), account, SETTINGS,
            as_of=AS_OF, event_calendar=events,
        )

        check = next(item for item in verdict.checks if item["check"] == "event_assignment_risk")
        self.assertFalse(check["passed"])
        self.assertFalse(verdict.admitted)


if __name__ == "__main__":
    unittest.main()
