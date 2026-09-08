"""Custom FastAPI entrypoint.

Built as a custom FastAPI app (per the ADK docs' "embedding an agent within
a custom FastAPI application" pattern for Cloud Run) rather than the bare
`adk deploy cloud_run` CLI, because this app needs its own Firebase ID
token + App Check verification at the HTTP boundary before the agent ever
runs — mirroring server.ts's middleware chain (verifyAuth, verifyAppCheck)
rather than relying on ADK's own request handling for auth.

Route-to-route mapping against the baseline:
- POST /api/journal/interaction  <- server.ts:420-602
- GET  /api/journal/quota        <- server.ts:391-409 (auth only, no App Check — matches the baseline)
- GET  /api/journal/history      <- server.ts:411-418 (auth only)
- GET  /api/config                <- server.ts:376-389 (public Firebase config only, no secrets)
- GET  /api/graph/relationships        <- new: direct accessor for app/tools/graph_tools.py's full graph
- GET  /api/graph/emotional-patterns   <- new: direct accessor for app/tools/sentiment_graph_tools.py's full graph

The two /api/graph/* routes are the first genuinely cross-origin traffic
this service answers — ../src/'s frontend calls them directly rather than
through server.ts (a deliberate, narrow exception to this prototype's
"never touch src/" boundary; server.ts itself is still untouched) — so this
is also the first place CORSMiddleware is needed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent import AgentDependencies, build_chat_model, build_root_agent
from app.callbacks.guardrails import CapacityExhaustedError, assert_current_app_check, reserve_model_attempt
from app.config import (
    BODY_LIMIT_BYTES,
    FEATHERLESS_MODEL,
    HISTORY_INVOCATIONS_TO_KEEP,
    IDENTIFIER_PATTERN,
    SUMMARY_INPUT_TOKEN_CAP,
    SUMMARY_OUTPUT_TOKEN_CAP,
    SUMMARY_TITLE_MAX_BYTES,
    cors_allowed_origins,
    is_test_bypass_enabled,
    project_id,
)
from app.memory.firestore_memory_service import FirestoreMemoryService
from app.models.embedding_client import GeminiEmbeddingClient
from app.security.app_check import AppCheckError, build_default_verifier
from app.security.firebase_auth import AuthError, verify_authorization_header
from app.sentiment.extraction import build_extraction_instruction, build_extraction_source, parse_extraction_response
from app.sentiment.vocabulary import load_trigger_vocabulary
from app.text_limits import first_text_part, token_upper_bound, truncate_to_token_cap
from app.tools.graph_tools import get_full_relationship_graph
from app.tools.journal_tools import JournalToolError, persist_completed_interaction, record_sentiment_analysis
from app.tools.sentiment_graph_tools import get_full_emotional_pattern_graph

_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_firebase_config() -> dict:
    # FIREBASE_APPLET_CONFIG_PATH overrides the default derived-from-
    # directory-depth path (_REPO_ROOT, i.e. parents[2] of this file) —
    # that arithmetic only holds if a deployment mirrors this repo's exact
    # nesting depth (adk-agent/Dockerfile does, deliberately; see its
    # comments), which is fragile to any future layout change. Since a
    # broken mapping fails *silently* here (empty config, not an error),
    # an explicit override is a cheap safety valve without changing the
    # default behavior for anyone not setting it. Found by /simplify.
    override = os.environ.get("FIREBASE_APPLET_CONFIG_PATH")
    config_path = Path(override) if override else _REPO_ROOT / "firebase-applet-config.json"
    try:
        return json.loads(config_path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 - never crash the app over an optional config file
        return {}


def _safe_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _capacity_error_status(error: CapacityExhaustedError) -> int:
    return 429 if error.code == "ATTEMPT_CAPACITY" else 401


def _default_firebase_app_exists() -> bool:
    try:
        import firebase_admin

        firebase_admin.get_app()
        return True
    except ValueError:
        return False


def create_app(*, db: Any, auth_client: Any, now=lambda: datetime.now(timezone.utc)) -> FastAPI:
    """Dependency-injectable factory, mirroring createApp(dependencies) in
    server.ts — tests build this with fake db/auth_client, never real
    credentials (docs/security-test-plan.md's "no real credential in tests"
    rule)."""

    firebase_config = _read_firebase_config()
    pid = project_id()
    embedding_client = GeminiEmbeddingClient()
    memory_service = FirestoreMemoryService(firestore_client=db, embedding_client=embedding_client)

    # None only when the Admin SDK's default app isn't initialized (e.g. unit
    # tests using a fake db/auth_client with no real Firebase app) — see
    # _require_app_check's is_test_bypass_enabled() fallback for that case.
    app_check_verifier = build_default_verifier() if _default_firebase_app_exists() else None

    from app.tools.journal_tools import build_journal_tools

    _save_journal_interaction_tool, get_quota_status_tool = build_journal_tools(db=db, now=now)

    fastapi_app = FastAPI(title="Secure Journal ADK Agent (prototype)")
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allowed_origins(),
        allow_credentials=False,  # Bearer tokens in headers, not cookies — no credentialed CORS needed
        allow_methods=["GET"],
        allow_headers=["Authorization"],
    )

    def _current_app_check_token(request: Request) -> Optional[str]:
        return request.headers.get("x-firebase-appcheck") or request.headers.get("x-firebase-app-check")

    def _require_auth(authorization: Optional[str]):
        try:
            return verify_authorization_header(authorization, auth_client=auth_client, project_id=pid)
        except AuthError:
            raise HTTPException(status_code=401, detail="Authentication is required.")

    def _require_app_check(request: Request):
        token = _current_app_check_token(request)
        if not token:
            raise HTTPException(status_code=401, detail="Valid App Check is required.")
        if app_check_verifier is None:
            if is_test_bypass_enabled():
                return
            raise HTTPException(status_code=401, detail="Valid App Check is required.")
        try:
            app_check_verifier.verify(token)
        except AppCheckError:
            raise HTTPException(status_code=401, detail="Valid App Check is required.")

    @fastapi_app.get("/api/config")
    async def get_config():
        return {
            "projectId": firebase_config.get("projectId", ""),
            "appId": firebase_config.get("appId", ""),
            "apiKey": firebase_config.get("apiKey", ""),
            "authDomain": firebase_config.get("authDomain", ""),
            "storageBucket": firebase_config.get("storageBucket", ""),
            "messagingSenderId": firebase_config.get("messagingSenderId", ""),
            "recaptchaSiteKey": firebase_config.get("recaptchaSiteKey", ""),
        }

    @fastapi_app.get("/api/journal/quota")
    async def get_quota(authorization: Optional[str] = Header(default=None)):
        user = _require_auth(authorization)
        try:
            status = get_quota_status_tool(_fake_tool_context(user.uid))
        except JournalToolError:
            return _safe_error(503, "Quota status is temporarily unavailable.")
        # Adapted to the frontend's camelCase QuotaStatus contract (src/types.ts);
        # the tool itself returns snake_case, which is more natural for the agent.
        return {
            "completedInteractionCount": status["completed_interaction_count"],
            "completedInteractionLimit": status["completed_interaction_limit"],
            "monthlyAttemptCount": status["monthly_attempt_count"],
            "monthlyAttemptLimit": status["monthly_attempt_limit"],
            "cooldownSeconds": status["cooldown_seconds"],
        }

    @fastapi_app.get("/api/journal/history")
    async def get_history(authorization: Optional[str] = Header(default=None)):
        user = _require_auth(authorization)
        try:
            docs = (
                db.collection(f"users/{user.uid}/interactions")
                .order_by("timestamps.createdAt", direction="DESCENDING")
                .limit(10)
                .stream()
            )
            return [_public_interaction(doc.id, doc.to_dict() or {}) for doc in docs]
        except Exception:  # noqa: BLE001
            return _safe_error(503, "Journal history is temporarily unavailable.")

    @fastapi_app.get("/api/graph/relationships")
    async def get_relationship_graph(authorization: Optional[str] = Header(default=None)):
        user = _require_auth(authorization)
        try:
            return get_full_relationship_graph(db=db, uid=user.uid)
        except Exception:  # noqa: BLE001
            return _safe_error(503, "The relationship graph is temporarily unavailable.")

    @fastapi_app.get("/api/graph/emotional-patterns")
    async def get_emotional_pattern_graph(authorization: Optional[str] = Header(default=None)):
        user = _require_auth(authorization)
        try:
            return get_full_emotional_pattern_graph(db=db, uid=user.uid)
        except Exception:  # noqa: BLE001
            return _safe_error(503, "The emotional pattern graph is temporarily unavailable.")

    @fastapi_app.post("/api/journal/interaction")
    async def post_interaction(request: Request, authorization: Optional[str] = Header(default=None)):
        raw_body = await request.body()
        if len(raw_body) > BODY_LIMIT_BYTES:
            return _safe_error(413, "Request body exceeds the 4 KiB limit.")
        try:
            body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            return _safe_error(400, "Malformed JSON payload.")

        user = _require_auth(authorization)
        _require_app_check(request)

        submitted = _parse_interaction(body)
        if submitted is None:
            return _safe_error(400, "Invalid journal interaction payload.")
        prompt, session_id, idempotency_request_id = submitted

        deps = AgentDependencies(
            db=db,
            embedding_client=embedding_client,
            memory_service=memory_service,
            now=now,
            app_check_verifier=app_check_verifier,
            app_check_token_provider=lambda: _current_app_check_token(request),
        )
        try:
            # Building the agent constructs the Featherless model
            # (app/models/featherless_llm.py), which raises immediately if
            # FEATHERLESS_API_KEY is missing — that must land here, not
            # escape as an uncaught 500, so it stays inside this try block
            # rather than before it.
            agent, _save_tool, _quota_tool = build_root_agent(deps)
            chat_text = await _run_agent_turn(agent, uid=user.uid, session_id=session_id, prompt=prompt)
        except JournalToolError as error:
            return _safe_error(429 if error.code in ("RATE_LIMIT", "INTERACTION_CAPACITY") else 502, error.message)
        except CapacityExhaustedError as error:
            return _safe_error(_capacity_error_status(error), str(error))
        except Exception:  # noqa: BLE001
            return _safe_error(503, "The reflection service is temporarily unavailable.")

        try:
            extraction = await _generate_summary_and_sentiment(deps, uid=user.uid, prompt=prompt, chat_text=chat_text)
        except CapacityExhaustedError as error:
            return _safe_error(_capacity_error_status(error), str(error))
        except Exception:  # noqa: BLE001
            return _safe_error(503, "The summary service is temporarily unavailable.")
        summary_text, summary_model = extraction.title, extraction.model_used

        try:
            result = persist_completed_interaction(
                db=db,
                now=now,
                uid=user.uid,
                prompt=prompt,
                assistant_response=chat_text,
                automatic_session_summary=summary_text,
                model_used="secure_journal_agent",
                summary_model_used=summary_model,
                session_id=session_id,
                idempotency_request_id=idempotency_request_id,
            )
        except JournalToolError as error:
            status = {
                "IDEMPOTENCY_MISMATCH": 409,
                "INTERACTION_PENDING": 409,
                "RATE_LIMIT": 429,
                "INTERACTION_CAPACITY": 429,
            }.get(error.code, 503)
            return _safe_error(status, error.message)

        memory_service.embed_completed_interaction(
            uid=user.uid, interaction_id=result.interaction_id, summary=result.automatic_session_summary or summary_text,
        )
        record_sentiment_analysis(
            db=db,
            uid=user.uid,
            interaction_id=result.interaction_id,
            emotions=[{"label": emotion.label, "intensity": emotion.intensity} for emotion in extraction.emotions],
            triggers=[
                {"label": trigger.label, "phrase": trigger.phrase, "intensity": trigger.intensity}
                for trigger in extraction.triggers
            ],
            status=extraction.sentiment_status,
        )

        return {
            "id": result.interaction_id,
            "userPrompt": result.user_prompt,
            "assistantResponse": result.assistant_response,
            "automaticSessionSummary": result.automatic_session_summary,
            "sessionId": result.session_id,
            "turnOrder": result.turn_order,
            "status": result.status,
            "idempotencyRequestId": result.interaction_id,
        }

    @fastapi_app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        # Reshaped to this app's {"error": ...} contract (matching server.ts's
        # safeError, src/App.tsx's safeErrorMessage) instead of Starlette's
        # default {"detail": ...} body.
        return _safe_error(exc.status_code, str(exc.detail))

    @fastapi_app.exception_handler(Exception)
    async def catch_all(_request: Request, _exc: Exception):
        # A narrow final handler prevents framework stack traces from
        # reaching clients, mirroring server.ts:605.
        return _safe_error(500, "Internal server error.")

    return fastapi_app


def _parse_interaction(body: Any) -> Optional[tuple[str, str, str]]:
    if not isinstance(body, dict):
        return None
    permitted = {"prompt", "sessionId", "idempotencyRequestId"}
    if any(key not in permitted for key in body.keys()):
        return None
    prompt, session_id, idempotency_request_id = body.get("prompt"), body.get("sessionId"), body.get("idempotencyRequestId")
    if not isinstance(prompt, str) or not isinstance(session_id, str) or not isinstance(idempotency_request_id, str):
        return None
    normalized_prompt = prompt.strip()
    if not normalized_prompt or not _IDENTIFIER_RE.match(session_id) or not _IDENTIFIER_RE.match(idempotency_request_id):
        return None
    from app.config import CHAT_INPUT_TOKEN_CAP

    if token_upper_bound(normalized_prompt) > CHAT_INPUT_TOKEN_CAP:
        return None
    return normalized_prompt, session_id, idempotency_request_id


def _public_interaction(doc_id: str, data: dict) -> dict:
    fields = (
        "userPrompt", "assistantResponse", "automaticSessionSummary", "sessionId",
        "turnOrder", "modelUsed", "timestamps", "status", "idempotencyRequestId",
    )
    return {"id": doc_id, **{key: data.get(key) for key in fields if data.get(key) is not None}}


class _FakeToolContext:
    def __init__(self, uid: str):
        self.state = {"uid": uid}


def _fake_tool_context(uid: str) -> _FakeToolContext:
    return _FakeToolContext(uid)


async def _run_agent_turn(agent: Any, *, uid: str, session_id: str, prompt: str) -> str:
    from google.adk.plugins.context_filter_plugin import ContextFilterPlugin
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    # Bounds conversation-history growth turn over turn (see the
    # HISTORY_INVOCATIONS_TO_KEEP discussion in app/config.py) using ADK's
    # own history-trimming plugin — verified against the installed package
    # to actually exist and to preserve function-call/response pairing when
    # it trims, rather than reinventing token-budget trimming ourselves.
    runner = InMemoryRunner(
        agent=agent,
        app_name="secure_journal",
        plugins=[ContextFilterPlugin(num_invocations_to_keep=HISTORY_INVOCATIONS_TO_KEEP)],
    )
    await runner.session_service.create_session(app_name="secure_journal", user_id=uid, session_id=session_id)
    session = await runner.session_service.get_session(app_name="secure_journal", user_id=uid, session_id=session_id)
    session.state["uid"] = uid  # the only place a tool call's uid can come from — never a model argument

    content = types.Content(role="user", parts=[types.Part(text=f"<journal-data>{prompt}</journal-data>")])
    final_text = ""
    async for event in runner.run_async(user_id=uid, session_id=session_id, new_message=content):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text or final_text
    if not final_text:
        raise JournalToolError("MODEL_REJECTED", "The reflection response was incomplete.")
    return final_text


class _SummaryAndSentiment:
    """Plain result holder for _generate_summary_and_sentiment — deliberately
    not app.sentiment.extraction.ExtractionResult, since this also carries
    the model_used bookkeeping that module has no business knowing about."""

    def __init__(self, title: str, model_used: str, emotions: list, triggers: list, sentiment_status: str):
        self.title = title
        self.model_used = model_used
        self.emotions = emotions
        self.triggers = triggers
        self.sentiment_status = sentiment_status


async def _generate_summary_and_sentiment(
    deps: AgentDependencies, *, uid: str, prompt: str, chat_text: str
) -> _SummaryAndSentiment:
    """One combined call producing the auto-title and sentiment/trigger
    analysis together (see app/sentiment/extraction.py). The title is a hard
    requirement — a missing/unusable title raises, exactly as the old
    title-only _generate_summary did, so the existing SAVE-06 guarantee
    (no completed interaction without a summary) is unchanged. A malformed
    or missing sentiment portion does not raise; it degrades to
    sentiment_status "failed" so it can never regress that guarantee.
    """
    vocabulary = load_trigger_vocabulary(deps.db, uid)
    instruction = build_extraction_instruction(vocabulary)
    source = build_extraction_source(prompt, chat_text)
    budget = max(0, SUMMARY_INPUT_TOKEN_CAP - token_upper_bound(instruction))
    bounded_source = truncate_to_token_cap(source, budget)

    # This call happens outside the Runner, so it never goes through the
    # agent's before_model_callback — the same guardrail is asserted here
    # directly, exactly once, for this one attempt.
    assert_current_app_check(
        app_check_verifier=deps.app_check_verifier,
        app_check_token_provider=deps.app_check_token_provider,
    )
    reserve_model_attempt(db=deps.db, now=deps.now)

    extraction_model = build_chat_model(deps)
    request = _build_llm_request(
        instruction=instruction,
        text=f"<journal-data>{bounded_source}</journal-data>",
        max_output_tokens=SUMMARY_OUTPUT_TOKEN_CAP,
    )
    raw_text = ""
    async for response in extraction_model.generate_content_async(request):
        raw_text = first_text_part(getattr(response, "content", None)) or raw_text

    result = parse_extraction_response(raw_text, vocabulary=vocabulary)
    if not result.title:
        raise JournalToolError("SUMMARY_REJECTED", "The summary service returned an unusable response.")
    cleaned_title = truncate_to_token_cap(result.title.strip().strip("\"'"), SUMMARY_TITLE_MAX_BYTES) or "Journal reflection"

    return _SummaryAndSentiment(
        title=cleaned_title,
        model_used=FEATHERLESS_MODEL,
        emotions=result.emotions,
        triggers=result.triggers,
        sentiment_status=result.sentiment_status,
    )


def _build_llm_request(*, instruction: str, text: str, max_output_tokens: int):
    from google.adk.models import LlmRequest
    from google.genai import types

    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=types.GenerateContentConfig(system_instruction=instruction, max_output_tokens=max_output_tokens),
    )


def _build_production_dependencies():  # pragma: no cover - real-credential wiring, not exercised by tests
    import firebase_admin
    from firebase_admin import auth as firebase_auth_sdk
    from google.cloud import firestore

    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app()
    return firestore.Client(), firebase_auth_sdk


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _db, _auth_client = _build_production_dependencies()
    application = create_app(db=_db, auth_client=_auth_client)
    uvicorn.run(application, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
