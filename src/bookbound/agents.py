"""The model review layer.

WHY THIS IS SHAPED THE WAY IT IS
--------------------------------
The original design had an LLM *proposer* select a trade from a ranked
shortlist and an LLM *adversary* argue against it, with both required to agree.
Measured behaviour on real shortlists (see EVALUATION.md):

* 22 of 22 trials returned NO_TRADE, across two different shortlists.
* Removing one instruction from the proposer's prompt - "Prefer NO_TRADE when
  the shortlist offers no clear edge" - took selection from 0/6 to 6/6, so the
  refusal rate was measuring the prompt, not the trades.
* With a neutral prompt the proposer agreed with rank 1 in five of six trials.
  As a selector it was inert: it reproduced the deterministic ranking at the
  cost of an API call.
* The adversary then vetoed 6 of 6 otherwise-valid candidates.

So the pipeline could not reach an order in EITHER configuration, and the
component that blocked it contributed no selection signal.

Two design errors caused that:

1. **The model was asked to do the job code already does.** Selection among
   pre-priced, pre-gated, pre-ranked structures is arithmetic. The ranker is
   deterministic, testable and reproducible; a language model adding an opinion
   on top can only add variance. It did: 12 identical inputs produced 8
   distinct rationales, two of which contradicted each other.

2. **Both roles defaulted to refusal.** The adversary was told it was "rewarded
   for catching bad trades, not for approving good ones" and asked to reject
   when "the reasoning is generic" - a critique of RHETORIC, not of risk. A
   well-argued bad trade passes that test and a plainly-stated good one fails it.

The layer is now inverted:

* **Deterministic code selects.** Rank 1 of the gated, ranked shortlist is the
  trade. No model chooses what to trade, how much, or at what price.
* **The model reviews, with a narrow and objective mandate**: is there a
  specific, named hazard in the CONTEXT - the thing code genuinely cannot read -
  that makes this particular trade dangerous right now?
* **A veto must be substantiated.** It has to name an enumerated hazard and
  cite concrete evidence. An unsubstantiated veto is discarded and logged,
  because the deterministic gates have already done the arithmetic safety work
  and a vague objection is not a reason to override them.

This strictly REDUCES model authority: it can no longer decide what is traded,
only raise a documented objection to a decision code already made.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Settings
from .llm import LLMError, chat_json
from .structures import Structure

#: Hazards a language model can see and deterministic code cannot. A veto that
#: does not name one of these is not actionable: everything else - pricing,
#: sizing, liquidity, Greeks, concentration, expectancy - is already enforced
#: by gates that are unit-tested and cannot be argued with.
VETO_HAZARDS = (
    "EARNINGS_OR_EVENT",       # scheduled event inside the holding window
    "HEADLINE_RISK",           # a specific current headline about this underlier
    "REGIME_CONTRADICTION",    # trade direction contradicts explicit context data
    "DATA_SUSPECT",            # context/quote data is internally inconsistent
)

REVIEWER_SYSTEM = """You are the risk reviewer on a defined-risk options desk.

A deterministic system has ALREADY selected this trade. It priced it from live
quotes, sized it, and passed it through hard gates for max loss, portfolio
Greeks, concentration, liquidity, execution cost and expectancy. Those checks
are not yours to repeat and not yours to second-guess: you cannot see anything
about the arithmetic that the gates did not already enforce.

Your ONLY job is to catch what code cannot read: a specific, named hazard in the
market context or headlines that makes THIS trade dangerous right now.

Approve by default. The correct answer is APPROVE unless you can name a hazard
AND cite the concrete evidence for it from the context you were given.

Valid hazards, and nothing else counts:
- EARNINGS_OR_EVENT: a scheduled earnings or macro event falls inside the
  holding window and the structure is exposed to a gap through it.
- HEADLINE_RISK: a specific headline you were shown describes an event that
  directly threatens this underlier in this direction.
- REGIME_CONTRADICTION: the trade's direction directly contradicts an explicit
  number in the context (not a vibe, not a preference).
- DATA_SUSPECT: the context or quote data is internally inconsistent, so the
  trade rests on a number that cannot be right.

These are NOT valid reasons to reject, and you must not use them:
- "the reward/risk is poor" or "expected value is low" - gated already
- "the thesis is generic" or "no clear edge" - you are not grading prose
- "quotes are wide" or "liquidity" - gated already
- "volatility is uncertain" or general market caution - not specific
- the terminal_expiry_scenario numbers. That scenario is a terminal,
  expiry-only model output. It is NOT true expectancy or win probability
  unless it states BOTH `calibrated: true` and `policy-aware: true`, which it
  does not. Never cite it as evidence of edge, in either direction.

Reply with JSON only:
{"verdict": "APPROVE" | "REJECT",
 "hazard": "<one of EARNINGS_OR_EVENT|HEADLINE_RISK|REGIME_CONTRADICTION|DATA_SUSPECT, or null when approving>",
 "evidence": "<=240 chars quoting the specific context value or headline, or why it is safe>"}"""


@dataclass
class Decision:
    structure: Structure | None
    proposer: dict = field(default_factory=dict)
    adversary: dict = field(default_factory=dict)
    outcome: str = "NO_TRADE"   # SELECTED | NO_TRADE | VETOED
    detail: str = ""
    review_available: bool = True
    discarded_veto: dict | None = None

    @property
    def traded(self) -> bool:
        return self.outcome == "SELECTED" and self.structure is not None


_AMBIGUOUS_LEGACY_MODEL_FIELDS = frozenset({
    "expected_value_under_realised_vol",
    "ev_per_dollar_risked",
    "variance_premium",
    "probability_of_profit",
    "execution_drag",
    "ev_model",
})


def _agent_summary(candidate: Structure) -> dict:
    """Expose only the explicitly scoped form of model-derived metrics."""
    return {
        key: value
        for key, value in candidate.summary().items()
        if key not in _AMBIGUOUS_LEGACY_MODEL_FIELDS
    }


def _shortlist_payload(candidates: list[Structure], context: dict) -> str:
    """Serialised view of the gated shortlist, used for capture and review."""
    return json.dumps(
        {"context": context, "shortlist": [_agent_summary(c) for c in candidates]},
        indent=2,
        default=str,
    )


def select_candidate(candidates: list[Structure]) -> Structure | None:
    """The trade is rank 1 of the gated, ranked shortlist.

    Deterministic, reproducible and testable. The shortlist arrives already
    sorted by the ranker and every entry has passed every risk gate, so taking
    the front of it is a complete decision - not a default awaiting approval.
    """
    return candidates[0] if candidates else None


def _veto_is_substantiated(review: dict) -> bool:
    """A veto must name an enumerated hazard and cite evidence for it."""
    hazard = str(review.get("hazard") or "").strip().upper()
    evidence = str(review.get("evidence") or "").strip()
    return hazard in VETO_HAZARDS and len(evidence) >= 12


def review_trade(
    picked: Structure,
    candidates: list[Structure],
    context: dict,
    settings: Settings,
) -> dict:
    """Ask the reviewer for a substantiated objection to an already-made choice."""
    payload = json.dumps(
        {
            "context": context,
            "proposed_trade": _agent_summary(picked),
            "selection_method": (
                "deterministic rank 1 of a gated, ranked shortlist; all "
                "arithmetic risk checks already passed"
            ),
            "rejected_alternatives": [
                {"key": c.key,
                 "terminal_expiry_scenario": c.summary().get(
                     "terminal_expiry_scenario"),
                 "quality_score": c.summary().get("quality_score")}
                for c in candidates[:5] if c.key != picked.key
            ],
        },
        indent=2,
        default=str,
    )
    return chat_json(
        base_url=settings.featherless_base,
        api_key=settings.featherless_key,
        model=settings.adversary_model,
        system=REVIEWER_SYSTEM,
        user=payload,
        temperature=0.0,
        max_tokens=settings.adversary_max_tokens,
    )


def decide(
    candidates: list[Structure],
    context: dict,
    settings: Settings,
    *,
    propose_fn=None,
    review_fn=None,
) -> Decision:
    """Select deterministically, then invite a substantiated model veto.

    ``propose_fn`` is accepted for backward compatibility and ignored: selection
    is no longer delegated to a model.
    """
    del propose_fn

    picked = select_candidate(candidates)
    if picked is None:
        return Decision(None, outcome="NO_TRADE", detail="shortlist was empty")

    selection = {
        "choice": picked.key,
        "method": "deterministic_rank_1",
        "quality_score": picked.summary().get("quality_score"),
    }

    try:
        review = (review_fn or review_trade)(picked, candidates, context, settings)
    except LLMError as exc:
        # The reviewer is an ENHANCEMENT over complete deterministic gating, not
        # the safety system. Failing closed here is what made the pipeline
        # unable to trade at all: an unrelated inference outage silently became
        # a blanket refusal. Proceed, and record that the review is missing.
        if getattr(settings, "require_model_review", False):
            return Decision(
                None, selection, {}, "NO_TRADE",
                f"model review required but unavailable: {exc}",
                review_available=False,
            )
        return Decision(
            picked, selection, {}, "SELECTED",
            f"selected deterministically; model review unavailable ({exc})",
            review_available=False,
        )

    verdict = str(review.get("verdict", "")).strip().upper()
    if verdict == "REJECT":
        if _veto_is_substantiated(review):
            return Decision(
                None, selection, review, "VETOED",
                f"{review.get('hazard')}: {review.get('evidence')}",
            )
        # An objection the reviewer cannot ground in a named hazard does not
        # override gates that are unit-tested. Discard it, but keep it on the
        # record so a pattern of discarded vetoes is visible.
        return Decision(
            picked, selection, review, "SELECTED",
            "selected deterministically; unsubstantiated veto discarded",
            discarded_veto=review,
        )

    return Decision(
        picked, selection, review, "SELECTED",
        str(review.get("evidence") or "reviewer raised no hazard"),
    )
