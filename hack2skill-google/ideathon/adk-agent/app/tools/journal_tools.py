"""Journal persistence and quota tools.

**A deliberate design choice, different from a naive "agent decides whether
to save" demo**: this project's binding requirement (compliance matrix C-16;
docs/security-test-plan.md SAVE-01..07) is that a submitted input's output
is *never* silently lost — persistence cannot be contingent on the model
choosing to call a tool. So `persist_completed_interaction` below is a plain
function, not itself the tool: `app/main.py` calls it deterministically after
every turn regardless of what the model did, exactly mirroring how
server.ts's route handler unconditionally calls markCompleted
(server.ts:420-602) rather than leaving persistence to chance. It is *also*
exposed as the `save_journal_interaction` tool so the model can call it
explicitly (e.g. mid-conversation bookkeeping) — the idempotency key makes a
double call (model + orchestration) safe, never a duplicate write.

This is a prototype-grade port of the admission transaction in
server.ts:428-506, not a byte-for-byte clone: cross-month stale-pending
recovery is simplified to "treat any stale pending record as retryable
capacity, without adjusting a prior month's counters." That simplification
is called out here rather than silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.config import (
    CHAT_INPUT_TOKEN_CAP,
    COMPLETED_INTERACTION_LIMIT,
    IDENTIFIER_PATTERN,
    MONTHLY_ATTEMPT_LIMIT,
    PENDING_STALE_SECONDS,
    USER_COOLDOWN_SECONDS,
    default_transactional,
    month_key,
)
from app.text_limits import token_upper_bound

_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)


class JournalToolError(Exception):
    """Carries only a safe, user-facing message — never verifier/DB internals."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def require_uid(tool_context: Any) -> str:
    """Reads the uid app/main.py stashed in session state from the verified
    Firebase token. Deliberately never reads a uid from tool arguments —
    the model can never choose whose data it touches, mirroring
    server.ts:423 (`const uid = req.verifiedUser!.uid`).
    """
    uid = (getattr(tool_context, "state", None) or {}).get("uid")
    if not uid:
        raise JournalToolError("UNAUTHENTICATED", "Authentication is required.")
    return uid


@dataclass(frozen=True)
class PersistedInteraction:
    interaction_id: str
    duplicate: bool
    status: str
    user_prompt: str
    assistant_response: Optional[str]
    automatic_session_summary: Optional[str]
    session_id: str
    turn_order: int


def persist_completed_interaction(
    *,
    db: Any,
    now: Callable[[], datetime],
    uid: str,
    prompt: str,
    assistant_response: str,
    automatic_session_summary: str,
    model_used: str,
    summary_model_used: str,
    session_id: str,
    idempotency_request_id: str,
    transactional: Optional[Callable[[Callable], Callable]] = None,
) -> PersistedInteraction:
    """Admits capacity/idempotency, then writes the completed record.

    Ports the admission transaction (server.ts:428-506) plus markCompleted
    (server.ts:349-374) as one atomic Firestore transaction, since here the
    chat + summary text are already available at call time (produced by the
    agent's own turn before this function runs) rather than generated mid
    transaction as in the Node version.

    `transactional` defaults to the real `google.cloud.firestore.transactional`
    decorator and should be left at its default in production. Tests pass
    `transactional=lambda fn: fn` so the identical admission logic runs
    directly against a lightweight fake transaction, without needing to
    satisfy Firestore's real begin/commit/retry machinery.
    """
    prompt = prompt.strip()
    if not prompt:
        raise JournalToolError("INVALID_INPUT", "Journal entry cannot be empty.")
    if not _IDENTIFIER_RE.match(session_id) or not _IDENTIFIER_RE.match(idempotency_request_id):
        raise JournalToolError("INVALID_INPUT", "Invalid session or request identifier.")
    if token_upper_bound(prompt) > CHAT_INPUT_TOKEN_CAP:
        raise JournalToolError("INVALID_INPUT", "Journal entry exceeds the model input limit.")

    current = now()
    interaction_ref = db.document(f"users/{uid}/interactions/{idempotency_request_id}")
    user_ref = db.document(f"users/{uid}")
    limit_ref = db.document(f"serviceLimits/monthly/months/{month_key(current)}")
    session_query = (
        db.collection(f"users/{uid}/interactions")
        .where("sessionId", "==", session_id)
        .where("status", "==", "completed")
    )

    # The transactional body is a plain function of (transaction, refs...) so
    # it can be unit-tested directly against a lightweight fake transaction —
    # google-cloud-firestore's real `@firestore.transactional` decorator
    # drives actual GAPIC begin/commit/retry machinery (verified against the
    # installed package), which a fake can't safely stand in for. Only the
    # real production call site below wraps it with that decorator.
    def run(transaction) -> PersistedInteraction:
        return _admit_and_write(
            transaction,
            prompt=prompt,
            assistant_response=assistant_response,
            automatic_session_summary=automatic_session_summary,
            model_used=model_used,
            summary_model_used=summary_model_used,
            session_id=session_id,
            idempotency_request_id=idempotency_request_id,
            current=current,
            interaction_ref=interaction_ref,
            user_ref=user_ref,
            limit_ref=limit_ref,
            session_query=session_query,
        )

    transactional = transactional or default_transactional()

    return transactional(run)(db.transaction())


def _admit_and_write(
    transaction: Any,
    *,
    prompt: str,
    assistant_response: str,
    automatic_session_summary: str,
    model_used: str,
    summary_model_used: str,
    session_id: str,
    idempotency_request_id: str,
    current: datetime,
    interaction_ref: Any,
    user_ref: Any,
    limit_ref: Any,
    session_query: Any,
) -> PersistedInteraction:
    """The core admission + write logic, decoupled from Firestore's real
    transactional retry wrapper so it can be exercised directly in tests
    with a fake `transaction` exposing only `.get(ref)` / `.set(ref, data,
    merge=...)`. See persist_completed_interaction for how production wires
    the real decorator around this."""
    interaction_snap = interaction_ref.get(transaction=transaction)
    user_snap = user_ref.get(transaction=transaction)
    limit_snap = limit_ref.get(transaction=transaction)
    existing = interaction_snap.to_dict() if interaction_snap.exists else None

    if existing and (existing.get("userPrompt") != prompt or existing.get("sessionId") != session_id):
        raise JournalToolError("IDEMPOTENCY_MISMATCH", "This idempotency request ID belongs to different journal content.")
    if existing and existing.get("status") == "completed":
        return PersistedInteraction(
            interaction_id=idempotency_request_id,
            duplicate=True,
            status="completed",
            user_prompt=existing.get("userPrompt", prompt),
            assistant_response=existing.get("assistantResponse"),
            automatic_session_summary=existing.get("automaticSessionSummary"),
            session_id=existing.get("sessionId", session_id),
            turn_order=int(existing.get("turnOrder", 1)),
        )
    if existing and existing.get("status") == "pending":
        updated_at = existing.get("updatedAt")
        age_seconds = (current - updated_at).total_seconds() if updated_at else PENDING_STALE_SECONDS + 1
        if age_seconds < PENDING_STALE_SECONDS:
            raise JournalToolError("INTERACTION_PENDING", "This journal request is already being processed.")
        # Stale pending: fall through and re-admit (simplified relative
        # to server.ts's cross-month reservation reconciliation).

    limit_data = limit_snap.to_dict() or {}
    completed_count = int(limit_data.get("completedInteractionCount", 0))
    in_flight_count = int(limit_data.get("inFlightInteractionCount", 0))
    has_capacity = (completed_count + in_flight_count) < COMPLETED_INTERACTION_LIMIT

    is_fresh = existing is None
    if is_fresh:
        last_interaction_ms = int((user_snap.to_dict() or {}).get("lastInteractionMs", 0))
        elapsed_ms = current.timestamp() * 1000 - last_interaction_ms
        if elapsed_ms < USER_COOLDOWN_SECONDS * 1000:
            raise JournalToolError("RATE_LIMIT", "Please wait one minute before starting another journal interaction.")
    if not has_capacity:
        raise JournalToolError("INTERACTION_CAPACITY", "The monthly demo interaction capacity is exhausted.")

    turn_order = existing.get("turnOrder") if existing else None
    if turn_order is None:
        turn_order = len(list(session_query.get(transaction=transaction))) + 1

    transaction.set(limit_ref, {
        "month": month_key(current),
        "completedInteractionCount": completed_count + 1,
        "completedInteractionLimit": COMPLETED_INTERACTION_LIMIT,
        # This port never increments inFlightInteractionCount (unlike
        # server.ts, which has a separate reserve-then-complete phase this
        # simplified admission transaction collapses into one write — see
        # this file's module docstring) — always a pass-through, not a
        # decrement. Writing `max(0, in_flight_count - 0)` here was dead,
        # misleading no-op logic; found and removed by /simplify.
        "inFlightInteractionCount": in_flight_count,
        "monthlyAttemptLimit": MONTHLY_ATTEMPT_LIMIT,
        "updatedAt": current,
    }, merge=True)

    timestamps = (existing or {}).get("timestamps") or {
        "createdAt": current,
        "epochMs": int(current.timestamp() * 1000),
    }
    transaction.set(interaction_ref, {
        "userPrompt": prompt,
        "assistantResponse": assistant_response,
        "automaticSessionSummary": automatic_session_summary,
        "modelUsed": model_used,
        "summaryModelUsed": summary_model_used,
        "sessionId": session_id,
        "turnOrder": turn_order,
        "status": "completed",
        "idempotencyRequestId": idempotency_request_id,
        "interactionCapacityReserved": False,
        "capacityMonth": month_key(current),
        "timestamps": timestamps,
        "updatedAt": current,
    }, merge=bool(existing))

    if is_fresh:
        transaction.set(user_ref, {"lastInteractionMs": int(current.timestamp() * 1000), "updatedAt": current}, merge=True)

    return PersistedInteraction(
        interaction_id=idempotency_request_id,
        duplicate=False,
        status="completed",
        user_prompt=prompt,
        assistant_response=assistant_response,
        automatic_session_summary=automatic_session_summary,
        session_id=session_id,
        turn_order=turn_order,
    )


def record_sentiment_analysis(
    *,
    db: Any,
    uid: str,
    interaction_id: str,
    emotions: list[dict],
    triggers: list[dict],
    status: str,
) -> None:
    """Attaches sentiment/trigger analysis to an already-completed
    interaction, exactly the way `FirestoreMemoryService.embed_completed_
    interaction` attaches an embedding: a post-hoc `.update()`, not part of
    the admission transaction. This matters because the interaction may have
    been completed either by app/main.py's deterministic call or by the
    agent's own `save_journal_interaction` tool call — `_admit_and_write`
    returns early as a duplicate for an already-completed interaction and
    never touches new fields, so sentiment has to be attached afterward
    regardless of which path completed it first, not folded into that
    transaction.

    Takes plain dicts (not app.sentiment.extraction's dataclasses) so this
    module has no dependency on the sentiment package — callers convert.
    """
    db.document(f"users/{uid}/interactions/{interaction_id}").update({
        "sentimentAnalysis": {"emotions": emotions, "triggers": triggers},
        "sentimentStatus": status,
    })


def build_journal_tools(*, db: Any, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
    """Returns the ADK-tool-shaped callables, closing over db/now for testability."""

    def save_journal_interaction(
        prompt: str,
        assistant_response: str,
        automatic_session_summary: str,
        session_id: str,
        idempotency_request_id: str,
        model_used: str,
        summary_model_used: str,
        tool_context: Any,
    ) -> dict:
        """Persist one completed journal turn for the signed-in user, exactly once.

        Call this as the final step after producing a reflection and its
        automatic summary. Safe to call more than once with the same
        idempotency_request_id — repeats return the original saved result
        rather than creating a duplicate.
        """
        uid = require_uid(tool_context)
        result = persist_completed_interaction(
            db=db,
            now=now,
            uid=uid,
            prompt=prompt,
            assistant_response=assistant_response,
            automatic_session_summary=automatic_session_summary,
            model_used=model_used,
            summary_model_used=summary_model_used,
            session_id=session_id,
            idempotency_request_id=idempotency_request_id,
        )
        return {
            "status": "success",
            "interaction_id": result.interaction_id,
            "duplicate": result.duplicate,
            "turn_order": result.turn_order,
        }

    def get_quota_status(tool_context: Any) -> dict:
        """Report the service's remaining monthly interaction/attempt capacity
        and this user's cooldown, so the agent can explain capacity limits
        instead of guessing. Mirrors GET /api/journal/quota (server.ts:391-409).
        """
        uid = require_uid(tool_context)
        current = now()
        limit_snap = db.document(f"serviceLimits/monthly/months/{month_key(current)}").get()
        user_snap = db.document(f"users/{uid}").get()
        limit_data = limit_snap.to_dict() or {}
        completed = int(limit_data.get("completedInteractionCount", 0))
        attempts_used = int(limit_data.get("monthlyAttemptCount", 0))
        last_interaction_ms = int((user_snap.to_dict() or {}).get("lastInteractionMs", 0))
        cooldown_seconds = max(
            0,
            round((USER_COOLDOWN_SECONDS * 1000 - (current.timestamp() * 1000 - last_interaction_ms)) / 1000),
        )
        return {
            "completed_interaction_count": completed,
            "completed_interaction_limit": COMPLETED_INTERACTION_LIMIT,
            "monthly_attempt_count": attempts_used,
            "monthly_attempt_limit": MONTHLY_ATTEMPT_LIMIT,
            "cooldown_seconds": cooldown_seconds,
        }

    return save_journal_interaction, get_quota_status
