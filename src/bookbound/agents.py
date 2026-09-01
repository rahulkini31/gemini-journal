"""The two-model layer.

A Proposer argues for one structure from a shortlist it cannot write to. An
Adversary running a DIFFERENT model family argues against it. Both must agree,
or the cycle resolves to NO TRADE.

The models never see an untradeable structure, never choose a size, and never
touch an order. They rank; deterministic code decides.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .config import Settings
from .llm import LLMError, chat_json
from .structures import Structure

PROPOSER_SYSTEM = """You are a disciplined options analyst on a defined-risk desk.
You are given a SHORTLIST of pre-validated, pre-priced vertical spreads (both
debit and credit) plus market context: recent price action, realised volatility,
where spot sits in its 20-day range, and current headlines. Choose AT MOST ONE,
or choose NO_TRADE.

Hard rules:
- You may ONLY choose a `key` that appears verbatim in the shortlist.
- You may not choose size, strikes, or prices. Those are already fixed.
- NO_TRADE is a legitimate and frequently correct answer. Prefer it when the
  shortlist offers no clear edge.
- Ground your thesis in the CONTEXT, not just the shortlist arithmetic. "Best
  reward/risk" is not a thesis; it is a sort order. Cite the specific evidence:
  range position, 5/20-day move, realised vol versus the contract's implied vol,
  or a headline.
- Compare implied vol to realised vol. IV well above RV favours SELLING premium
  (credit structures); IV below RV favours BUYING it (debit structures).
- If you pass over a candidate with a better expected value, say why.

Reply with JSON only:
{"choice": "<key from the shortlist or NO_TRADE>",
 "confidence": <0.0-1.0>,
 "thesis": "<=240 chars, the specific reason>"}"""

ADVERSARY_SYSTEM = """You are the risk officer on an options desk. Your job is to
find the reason a proposed trade is a BAD idea. You have veto power and you are
rewarded for catching bad trades, not for approving good ones.

You are given the proposed trade in full, the analyst's thesis, and the
alternatives they passed over.

Be brief. Emit the JSON object and nothing else - do not think out loud.

Approve ONLY if the thesis is specific and supported by the numbers shown. Reject
if the reasoning is generic, the reward/risk is poor, the quotes are wide, the
implied vol relationship contradicts the thesis, or the thesis would apply
equally to any other candidate.

Reply with JSON only:
{"verdict": "APPROVE" | "REJECT",
 "reason": "<=240 chars, the specific objection or the specific support>"}"""


@dataclass
class Decision:
    structure: Structure | None
    proposer: dict
    adversary: dict
    outcome: str          # SELECTED | NO_TRADE | VETOED | LLM_ERROR
    detail: str

    @property
    def traded(self) -> bool:
        return self.outcome == "SELECTED" and self.structure is not None


def _shortlist_payload(candidates: list[Structure], context: dict) -> str:
    return json.dumps(
        {"context": context, "shortlist": [c.summary() for c in candidates]},
        indent=2,
        default=str,
    )


def decide(
    candidates: list[Structure],
    context: dict,
    settings: Settings,
    *,
    propose_fn=None,
) -> Decision:
    """Run proposer then adversary. Any failure resolves to NO TRADE."""
    if not candidates:
        return Decision(None, {}, {}, "NO_TRADE", "shortlist was empty")

    by_key = {c.key: c for c in candidates}
    payload = _shortlist_payload(candidates, context)

    # --- proposer ---------------------------------------------------------
    try:
        proposal = (propose_fn or _default_proposer)(payload, settings)
    except LLMError as exc:
        return Decision(None, {}, {}, "LLM_ERROR", f"proposer failed: {exc}")

    choice = str(proposal.get("choice", "NO_TRADE")).strip()
    if choice == "NO_TRADE":
        return Decision(None, proposal, {}, "NO_TRADE",
                        proposal.get("thesis", "proposer declined"))

    # the model may only name a key we generated - anything else is discarded
    if choice not in by_key:
        return Decision(None, proposal, {}, "NO_TRADE",
                        f"proposer returned a key not on the shortlist: {choice!r}")

    picked = by_key[choice]

    # --- adversary --------------------------------------------------------
    review_payload = json.dumps(
        {
            "context": context,
            "proposed_trade": picked.summary(),
            "analyst_thesis": proposal.get("thesis", ""),
            "analyst_confidence": proposal.get("confidence"),
            "rejected_alternatives": [
                {"key": c.key, "expected_value": c.summary()["expected_value"]}
                for c in candidates[:6] if c.key != picked.key
            ],
        },
        indent=2,
        default=str,
    )
    try:
        review = chat_json(
            base_url=settings.featherless_base,
            api_key=settings.featherless_key,
            model=settings.adversary_model,
            system=ADVERSARY_SYSTEM,
            user=review_payload,
            temperature=0.1,
            max_tokens=settings.adversary_max_tokens,
        )
    except LLMError as exc:
        # A silent adversary must not become an implicit approval.
        return Decision(None, proposal, {}, "LLM_ERROR",
                        f"adversary unavailable, failing closed: {exc}")

    if str(review.get("verdict", "")).upper() != "APPROVE":
        return Decision(None, proposal, review, "VETOED",
                        review.get("reason", "adversary rejected"))

    return Decision(picked, proposal, review, "SELECTED",
                    proposal.get("thesis", ""))


def _default_proposer(payload: str, settings: Settings) -> dict:
    """Proposer runs on Featherless too, on a separate model from the adversary.

    Swap this for a Claude call when running under an agent harness; the
    contract is (payload, settings) -> dict.
    """
    return chat_json(
        base_url=settings.featherless_base,
        api_key=settings.featherless_key,
        model=settings.proposer_model,
        system=PROPOSER_SYSTEM,
        user=payload,
        temperature=0.3,
    )
