"""Behavior specifications for the inverted decision layer.

The previous design could not reach an order in ANY configuration: the proposer
refused everything, and with the refusal bias removed the adversary vetoed
everything. These scenarios pin the properties that fix must have, so the
pipeline cannot silently return to a blanket refusal.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookbound import agents  # noqa: E402
from bookbound.config import RiskBudget, Settings  # noqa: E402
from bookbound.llm import LLMError  # noqa: E402
from bookbound.market import Contract  # noqa: E402
from bookbound.structures import Structure  # noqa: E402

SETTINGS = Settings(api_key="k", secret_key="s", risk=RiskBudget())
EXPIRY = date.today() + timedelta(days=21)


def _contract(symbol: str, strike: float, delta: float) -> Contract:
    return Contract(
        symbol=symbol, underlying="SPY", expiry=EXPIRY, kind="C", strike=strike,
        bid=3.00, ask=3.10, delta=delta, gamma=.02, theta=-.4, vega=.1, iv=.20,
    )


def structure(label: str) -> Structure:
    return Structure(
        name="bull_call_spread", underlying="SPY", expiry=EXPIRY, qty=1,
        long=_contract(f"L{label}", 760.0, .50),
        short=_contract(f"S{label}", 765.0, .30),
        net_debit=2.0, max_loss=200.0, max_gain=300.0,
        spot=762.0, realised_vol=.18, forward=762.5,
    )


class DescribeDeterministicSelection(unittest.TestCase):
    def test_given_a_ranked_shortlist_then_rank_one_is_selected(self):
        first, second = structure("A"), structure("B")

        self.assertIs(agents.select_candidate([first, second]), first)

    def test_given_an_empty_shortlist_then_nothing_is_selected(self):
        self.assertIsNone(agents.select_candidate([]))

    def test_given_identical_input_then_selection_is_reproducible(self):
        """The old proposer produced 8 distinct rationales for 12 identical
        inputs. Selection must not vary at all."""
        shortlist = [structure("A"), structure("B"), structure("C")]
        picks = {
            agents.decide(
                shortlist, {}, SETTINGS,
                review_fn=lambda *a, **k: {"verdict": "APPROVE"},
            ).structure.key
            for _ in range(20)
        }
        self.assertEqual(len(picks), 1)


class DescribeModelReview(unittest.TestCase):
    def _decide(self, review):
        return agents.decide(
            [structure("A"), structure("B")], {"headlines": []}, SETTINGS,
            review_fn=lambda *a, **k: review,
        )

    def test_given_approval_then_the_trade_is_selected(self):
        decision = self._decide({"verdict": "APPROVE", "evidence": "no hazard found"})

        self.assertEqual(decision.outcome, "SELECTED")
        self.assertTrue(decision.traded)

    def test_given_a_substantiated_veto_then_the_trade_is_refused(self):
        decision = self._decide({
            "verdict": "REJECT",
            "hazard": "EARNINGS_OR_EVENT",
            "evidence": "SPY component earnings land inside the 21-day window",
        })

        self.assertEqual(decision.outcome, "VETOED")
        self.assertFalse(decision.traded)
        self.assertIn("EARNINGS_OR_EVENT", decision.detail)

    def test_given_an_unsubstantiated_veto_then_it_is_discarded_and_recorded(self):
        """The exact failure that blocked every trade: a veto for vague reasons.

        Gates that are unit-tested are not overridden by an objection the
        reviewer cannot ground in a named hazard - but it stays on the record."""
        decision = self._decide({"verdict": "REJECT", "reason": "no clear edge"})

        self.assertEqual(decision.outcome, "SELECTED")
        self.assertIsNotNone(decision.discarded_veto)

    def test_given_a_named_hazard_without_evidence_then_it_is_discarded(self):
        decision = self._decide({"verdict": "REJECT", "hazard": "HEADLINE_RISK",
                                 "evidence": "bad"})

        self.assertEqual(decision.outcome, "SELECTED")

    def test_given_an_unknown_hazard_then_it_is_discarded(self):
        decision = self._decide({
            "verdict": "REJECT", "hazard": "VIBES",
            "evidence": "this trade feels wrong to me right now",
        })

        self.assertEqual(decision.outcome, "SELECTED")


class DescribeReviewerOutage(unittest.TestCase):
    def _decide(self, settings):
        def boom(*args, **kwargs):
            raise LLMError("inference provider unreachable")
        return agents.decide(
            [structure("A")], {}, settings, review_fn=boom,
        )

    def test_given_reviewer_outage_then_deterministic_selection_still_trades(self):
        """An inference outage must not become a blanket refusal: the reviewer
        is an enhancement over complete gating, not the safety system."""
        decision = self._decide(SETTINGS)

        self.assertEqual(decision.outcome, "SELECTED")
        self.assertFalse(decision.review_available)
        self.assertIn("unavailable", decision.detail)

    def test_given_review_is_required_then_outage_refuses(self):
        import dataclasses
        strict = dataclasses.replace(SETTINGS, require_model_review=True)

        decision = self._decide(strict)

        self.assertEqual(decision.outcome, "NO_TRADE")
        self.assertFalse(decision.review_available)


class DescribeModelAuthorityLimits(unittest.TestCase):
    def test_the_model_cannot_choose_which_trade_is_taken(self):
        """Selection is not delegated. A reviewer naming a different candidate
        cannot change what was selected."""
        first, second = structure("A"), structure("B")
        decision = agents.decide(
            [first, second], {}, SETTINGS,
            review_fn=lambda *a, **k: {"verdict": "APPROVE", "choice": second.key},
        )

        self.assertIs(decision.structure, first)

    def test_a_legacy_propose_fn_is_ignored_rather_than_obeyed(self):
        called = []

        def legacy(payload, settings):
            called.append(payload)
            return {"choice": "NO_TRADE"}

        decision = agents.decide(
            [structure("A")], {}, SETTINGS, propose_fn=legacy,
            review_fn=lambda *a, **k: {"verdict": "APPROVE"},
        )

        self.assertEqual(called, [])
        self.assertEqual(decision.outcome, "SELECTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
