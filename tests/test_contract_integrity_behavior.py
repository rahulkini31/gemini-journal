"""Behavior specifications for option-contract deliverable integrity.

The market-data snapshot identifies an OCC symbol and quote, but it does not
describe the contract deliverable.  These scenarios require an independent
match against Alpaca's option-contract master before a quote can become an
entry candidate or be refreshed for submission.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound.config import PAPER_HOST, RiskBudget, Settings  # noqa: E402
from bookbound.market import (  # noqa: E402
    Contract,
    _with_contract_metadata,
    option_chain,
    option_snapshots,
    standard_contract_is_verified,
    tradeable,
)


SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())
AS_OF = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
SYMBOL = "SPY260925C00760000"


def snapshot(symbol: str = SYMBOL) -> dict:
    del symbol
    return {
        "greeks": {"delta": 0.4, "gamma": 0.02, "theta": -0.5, "vega": 0.1},
        "impliedVolatility": 0.2,
        "latestQuote": {
            "bp": 1.00,
            "ap": 1.08,
            "bs": 10,
            "as": 12,
            "t": "2026-09-02T14:59:30Z",
        },
    }


def unverified_contract() -> Contract:
    return Contract(
        symbol=SYMBOL,
        underlying="SPY",
        expiry=date(2026, 9, 25),
        kind="C",
        strike=760.0,
        bid=1.00,
        ask=1.08,
        delta=0.4,
        gamma=0.02,
        theta=-0.5,
        vega=0.1,
        iv=0.2,
        quote_timestamp=datetime(2026, 9, 2, 14, 59, 30, tzinfo=timezone.utc),
        bid_size=10,
        ask_size=12,
    )


def master_row(symbol: str = SYMBOL, **overrides) -> dict:
    row = {
        "id": "contract-id",
        "symbol": symbol,
        "name": "SPY Sep 25 2026 760 Call",
        "status": "active",
        "tradable": True,
        "expiration_date": "2026-09-25",
        "root_symbol": "SPY",
        "underlying_symbol": "SPY",
        "type": "call",
        "style": "american",
        "strike_price": "760",
        "multiplier": "100",
        "size": "100",
        "deliverables": [
            {
                "type": "equity",
                "symbol": "SPY",
                "asset_id": "underlier-id",
                "amount": "100",
                "allocation_percentage": "100",
                "settlement_type": "T+1",
                "settlement_method": "CCC",
                "delayed_settlement": False,
            }
        ],
    }
    row.update(overrides)
    return row


class TestContractMasterMatchingBehavior(unittest.TestCase):
    def test_given_standard_master_row_when_merged_then_contract_is_verified(self):
        enriched = _with_contract_metadata(unverified_contract(), master_row())

        self.assertIsNotNone(enriched)
        assert enriched is not None
        self.assertTrue(standard_contract_is_verified(enriched))
        self.assertEqual(enriched.multiplier, 100)
        self.assertEqual(enriched.contract_size, 100)
        self.assertEqual(enriched.deliverables[0].symbol, "SPY")

    def test_given_no_master_row_then_a_fresh_quote_still_fails_closed(self):
        quote = unverified_contract()

        self.assertFalse(standard_contract_is_verified(quote))
        self.assertFalse(tradeable(quote, SETTINGS, as_of=AS_OF))
        self.assertTrue(
            tradeable(quote, SETTINGS),
            "legacy non-entry diagnostics may still inspect a synthetic quote",
        )

    def test_given_adjusted_deliverables_then_contract_is_not_standard(self):
        adjusted = master_row(
            root_symbol="SPY1",
            deliverables=[
                {
                    "type": "equity",
                    "symbol": "SPY",
                    "asset_id": "underlier-id",
                    "amount": "50",
                    "allocation_percentage": "80",
                    "settlement_type": "T+1",
                    "settlement_method": "CCC",
                    "delayed_settlement": False,
                },
                {
                    "type": "cash",
                    "symbol": "USD",
                    "amount": "123.45",
                    "allocation_percentage": "20",
                    "settlement_type": "T+1",
                    "settlement_method": "CADF",
                    "delayed_settlement": False,
                },
            ],
        )

        enriched = _with_contract_metadata(unverified_contract(), adjusted)

        self.assertIsNotNone(enriched, "metadata remains available for diagnostics")
        assert enriched is not None
        self.assertFalse(standard_contract_is_verified(enriched))
        self.assertFalse(tradeable(enriched, SETTINGS, as_of=AS_OF))

    def test_given_missing_or_nonstandard_scalars_then_contract_is_rejected(self):
        missing_multiplier = _with_contract_metadata(
            unverified_contract(), master_row(multiplier=None),
        )
        mini = _with_contract_metadata(
            unverified_contract(), master_row(multiplier="10", size="10"),
        )

        self.assertIsNotNone(missing_multiplier)
        self.assertFalse(standard_contract_is_verified(missing_multiplier))
        self.assertIsNotNone(mini)
        self.assertFalse(standard_contract_is_verified(mini))

    def test_given_master_semantics_disagree_with_occ_then_merge_fails(self):
        mismatches = (
            {"underlying_symbol": "QQQ"},
            {"expiration_date": "2026-10-16"},
            {"type": "put"},
            {"strike_price": "761"},
        )

        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                self.assertIsNone(
                    _with_contract_metadata(
                        unverified_contract(), master_row(**mismatch),
                    )
                )


class TestContractMasterRetrievalBehavior(unittest.TestCase):
    def test_given_chain_pages_then_only_master_verified_standard_quotes_survive(self):
        adjusted_symbol = "SPY260925C00765000"
        quote_response = {
            "snapshots": {
                SYMBOL: snapshot(),
                adjusted_symbol: snapshot(adjusted_symbol),
            }
        }
        standard_page = {
            "option_contracts": [master_row()],
            "next_page_token": "next",
        }
        adjusted_page = {
            "option_contracts": [
                master_row(
                    adjusted_symbol,
                    strike_price="765",
                    size="50",
                    multiplier="50",
                    deliverables=[{
                        "type": "equity",
                        "symbol": "SPY",
                        "asset_id": "underlier-id",
                        "amount": "50",
                        "allocation_percentage": "100",
                        "settlement_type": "T+1",
                        "settlement_method": "CCC",
                        "delayed_settlement": False,
                    }],
                )
            ],
            "next_page_token": None,
        }

        with patch(
            "bookbound.market.get_json",
            side_effect=[quote_response, standard_page, adjusted_page],
        ) as get:
            contracts = option_chain(
                SETTINGS, "SPY", spot=760.0, as_of=AS_OF,
            )

        self.assertEqual([contract.symbol for contract in contracts], [SYMBOL])
        first_master_call = get.call_args_list[1]
        self.assertEqual(
            first_master_call.args[0], f"{PAPER_HOST}/v2/options/contracts",
        )
        self.assertEqual(first_master_call.args[2]["show_deliverables"], "true")
        self.assertEqual(first_master_call.args[2]["status"], "active")
        self.assertEqual(first_master_call.args[2]["limit"], 10000)
        self.assertIsNone(first_master_call.args[2]["page_token"])
        self.assertEqual(get.call_args_list[2].args[2]["page_token"], "next")

    def test_given_exact_refresh_then_each_symbol_is_rematched_to_current_master(self):
        second = "SPY260925C00765000"
        response = {"snapshots": {SYMBOL: snapshot(), second: snapshot(second)}}

        with patch(
            "bookbound.market.get_json",
            side_effect=[
                response,
                master_row(),
                master_row(second, strike_price="765"),
            ],
        ) as get:
            refreshed = option_snapshots(SETTINGS, [SYMBOL, second])

        self.assertEqual(set(refreshed), {SYMBOL, second})
        self.assertTrue(all(standard_contract_is_verified(c) for c in refreshed.values()))
        self.assertEqual(
            get.call_args_list[1].args[0],
            f"{PAPER_HOST}/v2/options/contracts/{SYMBOL}",
        )
        self.assertEqual(
            get.call_args_list[2].args[0],
            f"{PAPER_HOST}/v2/options/contracts/{second}",
        )

    def test_given_nonstandard_exact_master_then_refresh_is_all_or_nothing(self):
        response = {"snapshots": {SYMBOL: snapshot()}}
        adjusted = master_row(size="50", multiplier="50")

        with patch(
            "bookbound.market.get_json", side_effect=[response, adjusted],
        ):
            with self.assertRaisesRegex(ValueError, "unverified or nonstandard"):
                option_snapshots(SETTINGS, [SYMBOL])


if __name__ == "__main__":
    unittest.main(verbosity=2)
