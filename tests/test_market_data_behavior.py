"""Behavior specifications for market-data provenance and freshness.

Each scenario is deliberately phrased as Given/When/Then so the safety rule is
readable without knowing the implementation.  No test in this module reaches
the network or reads credentials.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.context import (  # noqa: E402
    build,
    daily_bars,
    exchange_date,
    price_context,
)
from bookbound.market import (
    OptionDeliverable,  # noqa: E402
    Contract,
    UnderlyingSnapshot,
    _to_contract,
    cross_leg_quote_skew_seconds,
    option_chain,
    option_snapshots,
    quotes_are_synchronous,
    tradeable,
    underlying_price,
    underlying_snapshot,
    underlying_snapshot_is_fresh,
)


SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())


def snapshot(*, timestamp: str = "2026-09-01T19:59:59.654205861Z") -> dict:
    return {
        "greeks": {"delta": 0.4, "gamma": 0.02, "theta": -0.5, "vega": 0.1},
        "impliedVolatility": 0.2,
        "latestQuote": {
            "bp": 1.00,
            "ap": 1.08,
            "bs": 77,
            "as": 151,
            "bx": "U",
            "ax": "I",
            "c": "R",
            "t": timestamp,
        },
    }



def _master_row(symbol: str, **overrides) -> dict:
    """A standard 100-share equity-deliverable contract-master row."""
    row = {
        "symbol": symbol,
        "status": "active",
        "tradable": True,
        "root_symbol": "SPY",
        "underlying_symbol": "SPY",
        "style": "american",
        "multiplier": "100",
        "size": "100",
        "deliverables": [{
            "type": "equity", "symbol": "SPY", "amount": "100",
            "allocation_percentage": "100",
        }],
    }
    row.update(overrides)
    return row


def contract(**overrides) -> Contract:
    values = {
        "symbol": "SPY260918C00760000",
        "underlying": "SPY",
        "expiry": date(2026, 9, 18),
        "kind": "C",
        "strike": 760.0,
        "bid": 1.00,
        "ask": 1.08,
        "delta": 0.4,
        "gamma": 0.02,
        "theta": -0.5,
        "vega": 0.1,
        "iv": 0.2,
        "quote_timestamp": datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc),
        "bid_size": 10,
        "ask_size": 12,
        "bid_exchange": "U",
        "ask_exchange": "I",
        "quote_condition": "R",
        # A quote alone is not tradeable on the entry path: the contract master
        # must confirm a standard 100-share equity deliverable. These fixtures
        # describe an ordinary SPY contract, so they carry that verification.
        "multiplier": 100,
        "contract_size": 100,
        "contract_status": "active",
        "contract_tradable": True,
        "contract_style": "american",
        "root_symbol": "SPY",
        "deliverables": (
            OptionDeliverable(
                type="equity", symbol="SPY", amount=100.0,
                allocation_percentage=100.0,
            ),
        ),
        "metadata_verified": True,
    }
    values.update(overrides)
    return Contract(**values)


class TestAdjustedDailyBarBehavior(unittest.TestCase):
    def test_given_descending_api_data_when_requested_then_newest_bars_are_chronological(self):
        """Given a capped response, request newest adjusted bars, then reorder them."""
        api_bars = [
            {"t": "2026-09-01T04:00:00Z", "c": 103.0},
            {"t": "2026-08-31T04:00:00Z", "c": 102.0},
            {"t": "2026-08-30T04:00:00Z", "c": 101.0},
        ]
        as_of = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)

        with patch("bookbound.context.get_json", return_value={"bars": api_bars}) as get:
            bars = daily_bars(SETTINGS, "SPY", days=3, as_of=as_of)

        params = get.call_args.args[2]
        self.assertEqual(params["sort"], "desc")
        self.assertEqual(params["adjustment"], "all")
        self.assertEqual(params["limit"], 3)
        self.assertEqual([bar["c"] for bar in bars], [101.0, 102.0, 103.0])

    def test_given_timestamped_bars_when_context_is_built_then_latest_bar_time_is_exposed(self):
        bars = [
            {"t": f"2026-08-{day:02d}T04:00:00Z", "c": 700.0 + day}
            for day in range(20, 25)
        ]

        context = price_context(bars)

        self.assertEqual(context["latest_bar_timestamp"], "2026-08-24T04:00:00Z")


class TestExchangeDateBehavior(unittest.TestCase):
    def test_given_an_instant_after_midnight_utc_then_exchange_date_remains_prior_day(self):
        as_of = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)

        self.assertEqual(exchange_date(as_of), date(2026, 9, 1))

    def test_given_a_naive_datetime_then_exchange_date_refuses_ambiguous_input(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            exchange_date(datetime(2026, 9, 2, 9, 30))

    def test_given_an_explicit_instant_when_dte_is_computed_then_new_york_date_is_used(self):
        as_of = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)

        self.assertEqual(contract().dte(as_of=as_of), 17)

    def test_given_one_cycle_as_of_when_context_is_built_then_it_is_exposed_and_reused(self):
        as_of = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)
        bars = [{"t": f"2026-08-{day:02d}T04:00:00Z", "c": 700 + day}
                for day in range(20, 25)]

        with (
            patch("bookbound.context.daily_bars", return_value=bars) as get_bars,
            patch("bookbound.context.recent_news", return_value=[]) as get_news,
        ):
            context = build(SETTINGS, {}, as_of=as_of)

        self.assertEqual(context["as_of"], "2026-09-02T01:30:00+00:00")
        self.assertEqual(context["exchange_date"], "2026-09-01")
        for call in get_bars.call_args_list:
            self.assertIs(call.kwargs["as_of"], as_of)
        self.assertIs(get_news.call_args.kwargs["as_of"], as_of)


class TestOptionQuoteProvenanceBehavior(unittest.TestCase):
    def test_given_a_complete_snapshot_then_quote_provenance_is_preserved(self):
        parsed = _to_contract("SPY260918C00760000", snapshot())

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            parsed.quote_timestamp,
            datetime(2026, 9, 1, 19, 59, 59, 654205, tzinfo=timezone.utc),
        )
        self.assertEqual((parsed.bid_size, parsed.ask_size), (77, 151))
        self.assertEqual((parsed.bid_exchange, parsed.ask_exchange), ("U", "I"))
        self.assertEqual(parsed.quote_condition, "R")

    def test_given_current_sized_quote_when_checked_as_of_then_it_is_tradeable(self):
        as_of = datetime(2026, 9, 1, 14, 31, tzinfo=timezone.utc)

        self.assertTrue(tradeable(contract(), SETTINGS, as_of=as_of))

    def test_given_stale_or_size_less_quote_when_checked_as_of_then_it_fails_closed(self):
        as_of = datetime(2026, 9, 1, 14, 35, tzinfo=timezone.utc)

        self.assertFalse(tradeable(contract(), SETTINGS, as_of=as_of))
        self.assertFalse(
            tradeable(
                replace(
                    contract(),
                    quote_timestamp=as_of - timedelta(seconds=10),
                    bid_size=0,
                ),
                SETTINGS,
                as_of=as_of,
            )
        )

    def test_given_contract_outside_configured_dte_then_local_filter_rejects_it(self):
        as_of = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
        expired = replace(contract(), expiry=date(2026, 9, 1), quote_timestamp=as_of)
        too_near = replace(contract(), expiry=date(2026, 9, 10), quote_timestamp=as_of)
        too_far = replace(contract(), expiry=date(2026, 12, 1), quote_timestamp=as_of)

        self.assertFalse(tradeable(expired, SETTINGS, as_of=as_of))
        self.assertFalse(tradeable(too_near, SETTINGS, as_of=as_of))
        self.assertFalse(tradeable(too_far, SETTINGS, as_of=as_of))

    def test_given_legacy_contract_when_no_as_of_is_requested_then_old_filter_is_unchanged(self):
        legacy = replace(
            contract(),
            quote_timestamp=None,
            bid_size=None,
            ask_size=None,
        )

        self.assertTrue(tradeable(legacy, SETTINGS))

    def test_given_two_legs_then_timestamp_skew_is_measured_and_bounded(self):
        first = contract()
        second = replace(first, quote_timestamp=first.quote_timestamp + timedelta(seconds=2))

        self.assertEqual(cross_leg_quote_skew_seconds([first, second]), 2.0)
        self.assertTrue(quotes_are_synchronous([first, second], max_skew_seconds=2.0))
        self.assertFalse(quotes_are_synchronous([first, second], max_skew_seconds=1.0))
        self.assertFalse(
            quotes_are_synchronous(
                [first, replace(second, quote_timestamp=None)], max_skew_seconds=2.0
            )
        )


class TestUnderlyingQuoteProvenanceBehavior(unittest.TestCase):
    def test_given_latest_trade_then_price_timestamp_and_source_are_preserved(self):
        response = {
            "latestTrade": {
                "p": 760.25,
                "t": "2026-09-02T15:00:00.123456Z",
            },
            "dailyBar": {
                "c": 759.0,
                "t": "2026-09-02T04:00:00Z",
            },
        }

        with patch("bookbound.market.get_json", return_value=response):
            observed = underlying_snapshot(SETTINGS, "SPY")

        self.assertEqual(observed, UnderlyingSnapshot(
            symbol="SPY",
            price=760.25,
            timestamp=datetime(
                2026, 9, 2, 15, 0, 0, 123456, tzinfo=timezone.utc,
            ),
            source="latest_trade",
        ))

    def test_given_prior_bar_fallback_during_entry_then_it_fails_closed(self):
        as_of = datetime(2026, 9, 2, 15, 0, 30, tzinfo=timezone.utc)
        response = {
            "dailyBar": {"c": 759.0, "t": "2026-09-02T04:00:00Z"},
            "prevDailyBar": {"c": 755.0, "t": "2026-09-01T04:00:00Z"},
        }

        with patch("bookbound.market.get_json", return_value=response):
            self.assertIsNone(
                underlying_price(
                    SETTINGS, "SPY", as_of=as_of, require_fresh=True,
                )
            )

    def test_given_stale_or_timestamp_less_trade_then_it_fails_closed(self):
        as_of = datetime(2026, 9, 2, 15, 5, tzinfo=timezone.utc)
        stale = UnderlyingSnapshot(
            "SPY", 760.0, as_of - timedelta(minutes=5), "latest_trade",
        )
        unknown = replace(stale, timestamp=None)

        self.assertFalse(underlying_snapshot_is_fresh(stale, SETTINGS, as_of))
        self.assertFalse(underlying_snapshot_is_fresh(unknown, SETTINGS, as_of))


class TestOptionChainAsOfBehavior(unittest.TestCase):
    def test_given_explicit_as_of_and_raw_sink_then_bounds_and_pages_are_reproducible(self):
        good = snapshot()
        invalid = {"greeks": None, "impliedVolatility": None, "latestQuote": {}}
        responses = [
            {
                "snapshots": {
                    "SPY260918C00760000": good,
                    "SPY260918C00761000": invalid,
                },
                "next_page_token": "page-2",
            },
            {"snapshots": {"SPY260918P00760000": good}},
            # option_chain matches every quote to the contract master before
            # returning it, so the mock must serve that page too.
            {
                "option_contracts": [
                    _master_row("SPY260918C00760000"),
                    _master_row("SPY260918P00760000", type="put"),
                ],
                "next_page_token": None,
            },
        ]
        captured: list[tuple[str, dict]] = []
        as_of = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)

        with patch("bookbound.market.get_json", side_effect=responses) as get:
            contracts = option_chain(
                SETTINGS,
                "SPY",
                spot=760.0,
                as_of=as_of,
                raw_sink=lambda underlying, rows: captured.append((underlying, rows)),
            )

        first_params = get.call_args_list[0].args[2]
        second_params = get.call_args_list[1].args[2]
        self.assertEqual(first_params["expiration_date_gte"], "2026-09-15")
        self.assertEqual(first_params["expiration_date_lte"], "2026-10-16")
        self.assertNotIn("as_of", first_params, "snapshot API rejects an as_of query")
        self.assertIsNone(first_params["page_token"])
        self.assertEqual(second_params["page_token"], "page-2")
        self.assertEqual(len(captured), 2)
        self.assertIn("SPY260918C00761000", captured[0][1], "capture precedes filtering")
        self.assertEqual(len(contracts), 2)

    def test_given_selected_symbols_then_targeted_snapshot_refreshes_every_leg(self):
        """The execution path must obtain a new full snapshot for exact symbols."""
        symbols = ["SPY260918C00760000", "SPY260918C00765000"]
        response = {"snapshots": {symbol: snapshot() for symbol in symbols}}

        with patch("bookbound.market.get_json", return_value=response) as get:
            # verify_contracts is exercised in test_contract_integrity_behavior;
            # this scenario is about the snapshot request itself.
            refreshed = option_snapshots(
                SETTINGS, symbols, verify_contracts=False,
            )

        self.assertEqual(set(refreshed), set(symbols))
        self.assertEqual(
            get.call_args.args[0],
            "https://data.alpaca.markets/v1beta1/options/snapshots",
        )
        self.assertEqual(get.call_args.args[2]["symbols"], ",".join(symbols))
        self.assertEqual(get.call_args.args[2]["feed"], "indicative")

    def test_given_missing_selected_snapshot_then_refresh_fails_closed(self):
        symbols = ["SPY260918C00760000", "SPY260918C00765000"]
        response = {"snapshots": {symbols[0]: snapshot()}}

        with patch("bookbound.market.get_json", return_value=response):
            with self.assertRaisesRegex(ValueError, "missing option snapshots"):
                option_snapshots(SETTINGS, symbols)


if __name__ == "__main__":
    unittest.main(verbosity=2)
