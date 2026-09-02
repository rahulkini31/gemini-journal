"""Behaviour specifications for model-derived terminal scenario metrics."""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound import agents  # noqa: E402
from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.market import Contract  # noqa: E402
from bookbound.structures import Structure  # noqa: E402


EXPIRY = date.today() + timedelta(days=30)
SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())


def candidate(*, model_inputs: bool = True) -> Structure:
    long = Contract(
        symbol="SPY_LONG", underlying="SPY", expiry=EXPIRY, kind="C",
        strike=100.0, bid=4.9, ask=5.1, delta=.45, gamma=.02,
        theta=-.2, vega=.1, iv=.2,
    )
    short = Contract(
        symbol="SPY_SHORT", underlying="SPY", expiry=EXPIRY, kind="C",
        strike=105.0, bid=2.9, ask=3.1, delta=.25, gamma=.01,
        theta=-.1, vega=.05, iv=.2,
    )
    return Structure(
        name="bull_call_spread", underlying="SPY", expiry=EXPIRY, qty=1,
        long=long, short=short, net_debit=2.2, max_loss=220,
        max_gain=280, spot=102.0 if model_inputs else 0.0,
        realised_vol=.18 if model_inputs else 0.0,
        forward=102.2 if model_inputs else 0.0,
        spot_source="underlying_spot" if model_inputs else "missing",
        realised_vol_source=(
            "historical_realised_vol" if model_inputs else "missing"
        ),
        forward_source="put_call_parity" if model_inputs else "missing",
    )


class TestTerminalScenarioSummary(unittest.TestCase):
    def test_given_valid_proxy_then_summary_declares_semantics_and_limitations(self):
        summary = candidate().summary()

        scenario = summary["terminal_expiry_scenario"]
        self.assertTrue(scenario["valid"])
        self.assertEqual(scenario["measure"], "physical_proxy_lognormal")
        self.assertEqual(scenario["horizon"]["type"], "expiry")
        self.assertEqual(scenario["horizon"]["date"], EXPIRY.isoformat())
        self.assertFalse(scenario["calibrated"])
        self.assertFalse(scenario["policy_aware"])
        self.assertIsInstance(scenario["expected_pnl"], float)
        self.assertIsInstance(scenario["profit_probability"], float)

    def test_given_missing_inputs_then_model_values_are_null_not_zero(self):
        summary = candidate(model_inputs=False).summary()

        scenario = summary["terminal_expiry_scenario"]
        self.assertFalse(scenario["valid"])
        self.assertIsNone(scenario["expected_pnl"])
        self.assertIsNone(scenario["profit_probability"])
        self.assertIsNone(scenario["breakeven"])
        # Compatibility fields must not turn unavailable model output into 0.
        self.assertIsNone(summary["expected_value_under_realised_vol"])
        self.assertIsNone(summary["probability_of_profit"])

    def test_pricing_result_exposes_nullable_horizon_specific_names(self):
        valid = candidate().terminal_expiry_scenario()
        invalid = candidate(model_inputs=False).terminal_expiry_scenario()

        self.assertEqual(valid.expected_pnl_at_expiry, valid.ev_under_rv)
        self.assertEqual(
            valid.profit_probability_at_expiry, valid.probability_of_profit
        )
        self.assertIsNone(invalid.expected_pnl_at_expiry)
        self.assertIsNone(invalid.profit_probability_at_expiry)
        self.assertEqual(valid.measure, "physical_proxy_lognormal")
        self.assertEqual(valid.horizon, "expiry")
        self.assertFalse(valid.calibrated)
        self.assertFalse(valid.policy_aware)


class TestAgentMetricSemantics(unittest.TestCase):
    def test_prompts_forbid_treating_uncalibrated_scenario_as_true_edge(self):
        proposer = agents.PROPOSER_SYSTEM.lower()
        adversary = agents.ADVERSARY_SYSTEM.lower()

        self.assertNotIn("better expected value", proposer)
        for prompt in (proposer, adversary):
            self.assertIn("calibrated", prompt)
            self.assertIn("policy-aware", prompt)
            self.assertIn("terminal", prompt)

    def test_llm_shortlist_omits_ambiguous_legacy_metric_names(self):
        payload = json.loads(agents._shortlist_payload([candidate()], {}))
        item = payload["shortlist"][0]

        self.assertIn("terminal_expiry_scenario", item)
        self.assertNotIn("expected_value_under_realised_vol", item)
        self.assertNotIn("probability_of_profit", item)
        self.assertNotIn("ev_per_dollar_risked", item)

    def test_adversary_alternatives_use_explicit_terminal_scenario_not_ev(self):
        item = candidate()
        alternative = replace(
            candidate(),
            long=replace(candidate().long, symbol="SPY_ALT_LONG"),
            short=replace(candidate().short, symbol="SPY_ALT_SHORT"),
        )
        captured: dict = {}

        def fake_proposer(payload, settings):
            return {"choice": item.key, "confidence": .5, "thesis": "specific"}

        def fake_adversary(**kwargs):
            captured.update(json.loads(kwargs["user"]))
            return {"verdict": "REJECT", "reason": "test"}

        with patch.object(agents, "chat_json", side_effect=fake_adversary):
            agents.decide([item, alternative], {}, SETTINGS, propose_fn=fake_proposer)

        alternatives = captured["rejected_alternatives"]
        self.assertEqual(len(alternatives), 1)
        self.assertNotIn("expected_value", alternatives[0])
        self.assertIn("terminal_expiry_scenario", alternatives[0])
        proposed = captured["proposed_trade"]
        self.assertIn("terminal_expiry_scenario", proposed)
        self.assertFalse(proposed["terminal_expiry_scenario"]["calibrated"])


if __name__ == "__main__":
    unittest.main()
