"""Guardrail callbacks wired onto the agent.

This project's threat model treats two things as non-negotiable that ADK
does not provide out of the box:

1. **Capacity reservation before every model attempt** — ports
   `reserveAttempt` (server.ts:284-306): a unit of the durable monthly
   attempt cap is reserved before the call is issued, not after.
2. **App Check re-assertion before every model attempt** — ports
   `assertCurrentAppCheck` (server.ts:277-282).

With the Gemini fallback ladder removed, each completed interaction now
makes exactly two model attempts — the main chat turn and the summary+
sentiment extraction — rather than up to eight. `reserve_model_attempt` is
the shared, attempt-agnostic reservation function both call sites use:
`build_before_model_callback` wires it as the chat turn's ADK
`before_model_callback` (verified against the installed google-adk 2.8.0:
`_handle_before_model_callback` awaits the callback directly with no
try/except around it, so raising here propagates cleanly out through
`runner.run_async`, exactly like the exhausted-ladder case did before this
change); `app/main.py::_generate_summary_and_sentiment` calls it directly
before its own, non-agent model call, since that call happens outside the
Runner and never goes through `before_model_callback` at all.

`before_tool_callback` is unchanged: a safety net, not the primary auth
boundary (that is app/main.py's HTTP-layer verification) — it refuses to
let any tool run if the session state never had a verified uid attached, so
a wiring mistake fails closed instead of open.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from app.config import MONTHLY_ATTEMPT_LIMIT, default_transactional, month_key
from app.security.app_check import AppCheckError, AppCheckVerifier


class CapacityExhaustedError(Exception):
    """Raised (with a numeric-free message) when a model attempt is refused
    before it is issued. `code` lets callers map to the right HTTP status
    without parsing the message string — "ATTEMPT_CAPACITY" (429, retry
    later) vs "APP_CHECK" (401, not a capacity problem at all)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def reserve_model_attempt(
    *,
    db: Any,
    now: Callable[[], Any],
    transactional: Optional[Callable[[Callable], Callable]] = None,
) -> None:
    """Reserves one unit of the durable monthly model-attempt cap, or raises
    CapacityExhaustedError. Shared by the chat turn's before_model_callback
    and the direct summary+sentiment call in app/main.py — the two model
    attempts a completed interaction now makes.

    `transactional` defaults to `app.config.default_transactional()` (the
    real `google.cloud.firestore.transactional` decorator), the same shared
    escape hatch `persist_completed_interaction` uses
    (app/tools/journal_tools.py): that decorator drives real GAPIC
    begin/commit/retry machinery a lightweight fake transaction can't safely
    satisfy, so tests pass `transactional=lambda fn: fn` to run this exact
    logic directly against a fake instead.
    """
    transactional = transactional or default_transactional()

    current = now()
    limit_ref = db.document(f"serviceLimits/monthly/months/{month_key(current)}")

    def _reserve(transaction):
        snapshot = limit_ref.get(transaction=transaction)
        used = int((snapshot.to_dict() or {}).get("monthlyAttemptCount", 0))
        if used >= MONTHLY_ATTEMPT_LIMIT:
            raise CapacityExhaustedError("ATTEMPT_CAPACITY", "The monthly demo model capacity is exhausted.")
        transaction.set(limit_ref, {
            "month": month_key(current),
            "monthlyAttemptCount": used + 1,
            "monthlyAttemptLimit": MONTHLY_ATTEMPT_LIMIT,
            "updatedAt": current,
        }, merge=True)

    transactional(_reserve)(db.transaction())


def assert_current_app_check(
    *,
    app_check_verifier: Optional[AppCheckVerifier],
    app_check_token_provider: Callable[[], Optional[str]],
) -> None:
    if app_check_verifier is None:
        return
    try:
        app_check_verifier.verify(app_check_token_provider())
    except AppCheckError as error:
        raise CapacityExhaustedError("APP_CHECK", "Valid App Check is required.") from error


def build_before_model_callback(
    *,
    db: Any,
    now: Callable[[], Any],
    app_check_verifier: Optional[AppCheckVerifier],
    app_check_token_provider: Callable[[], Optional[str]],
    transactional: Optional[Callable[[Callable], Callable]] = None,
) -> Callable[..., Any]:
    """Builds the agent's ADK `before_model_callback`. Signature matches
    ADK's exact call convention (verified in base_llm_flow.py: `callback(
    callback_context=..., llm_request=...)` — keyword arguments, both
    required) rather than the positional style shown in some ADK tutorial
    snippets.

    `transactional` is forwarded to `reserve_model_attempt` — see that
    function's docstring; left at its default in production.
    """

    async def before_model_callback(callback_context: Any, llm_request: Any) -> None:
        del callback_context, llm_request  # not needed for this guardrail
        assert_current_app_check(
            app_check_verifier=app_check_verifier,
            app_check_token_provider=app_check_token_provider,
        )
        reserve_model_attempt(db=db, now=now, transactional=transactional)
        return None  # None means "proceed" — see _handle_before_model_callback.

    return before_model_callback


async def before_tool_callback(tool: Any, args: dict, tool_context: Any) -> Optional[dict]:
    """Fails closed if a tool is about to run without a verified uid attached
    to session state — a wiring-bug safety net, not the primary auth check
    (see app/main.py for the HTTP-layer Firebase token/App Check verification
    that sets `tool_context.state["uid"]` in the first place).
    """
    del args
    uid = (getattr(tool_context, "state", None) or {}).get("uid")
    if not uid:
        return {"status": "error", "error_message": "Authentication is required."}
    return None
