"""Shared limits and constants.

Most of these were originally ported 1:1 from the deployed Node backend
(`server.ts`), sized for Gemini's free-tier cost profile
(docs/ai-studio-threat-model.md, docs/bom.md). The chat token caps and the
model itself have since moved off that profile — see "Featherless / Gemma"
below — and are now sized against Gemma 3 27B's real 32,768-token context
window instead.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

BODY_LIMIT_BYTES = 4 * 1024

COMPLETED_INTERACTION_LIMIT = 10
# Previously "ten interactions can each use four chat attempts and four
# summary attempts" (80 = 10*8), sized for the old 4-model Gemini fallback
# ladder. There is no ladder anymore — one completed interaction now spends
# exactly 2 model attempts (one chat call, one summary+sentiment call), so
# 80 is a generous ceiling relative to actual usage, not a tight one. Left
# unchanged rather than tightened, since resizing the quota model wasn't
# part of what was asked.
MONTHLY_ATTEMPT_LIMIT = 80
USER_COOLDOWN_SECONDS = 60
PENDING_STALE_SECONDS = 2 * 60

# Featherless / Gemma: the single model this agent now uses (see
# app/models/featherless_llm.py). Replaces the old Gemini 4-model fallback
# ladder — Featherless is an OpenAI-compatible inference host, wired through
# ADK's own LiteLlm connector rather than a custom BaseLlm.
FEATHERLESS_MODEL = "google/gemma-3-27b-it"
FEATHERLESS_API_BASE = "https://api.featherless.ai/v1"
# Confirmed live on Featherless with this context window (see the plan/chat
# discussion this was verified against) — the caps below are sized as a
# small fraction of it, not the whole thing, leaving headroom for the system
# instruction, tool schemas, and conversation history.
FEATHERLESS_MODEL_CONTEXT_WINDOW = 32_768

# Revised from the original Gemini-free-tier values (1_000 / 300): those were
# sized so system instruction + full history + prompt fit inside a 1_000-
# token *total* budget, which rejected any journal entry over roughly
# 150-220 words before the model was ever called. Gemma 3 27B's 32k window
# has ample room instead — these are generous per-message caps, not the
# whole budget. CHAT_OUTPUT_TOKEN_CAP is now actually wired to the chat
# call's generate_content_config (app/agent.py) — previously defined but
# unused, a real gap fixed alongside this revision, not just a number bump.
CHAT_INPUT_TOKEN_CAP = 7_000
CHAT_OUTPUT_TOKEN_CAP = 2_000

# How many recent turns (invocations) of conversation history to keep, via
# ADK's built-in ContextFilterPlugin (app/main.py::_run_agent_turn) — a
# turn-count bound rather than a token-count one, since ADK has no partial
# token-budget trimming primitive (only include_contents: "default"|"none",
# checked against the installed package). At roughly 800-1,500 tokens/turn
# (see the earlier context-window discussion), 20 turns stays comfortably
# inside the 32k window even with occasional tool-call traffic, while still
# growing far beyond what the old 1_000-token total budget ever allowed.
HISTORY_INVOCATIONS_TO_KEEP = 20

SUMMARY_INPUT_TOKEN_CAP = 1_500
SUMMARY_OUTPUT_TOKEN_CAP = 600
# A defensive per-field cap distinct from SUMMARY_OUTPUT_TOKEN_CAP (which now
# bounds the whole title+emotions+triggers JSON response, not just the title).
SUMMARY_TITLE_MAX_BYTES = 120

# Memory Threads / recall bounds, per docs/ai-studio-memory-threads-prompt.md.
MEMORY_EMBEDDING_MODEL = "gemini-embedding-2"
MEMORY_EMBEDDING_DIMENSIONS = 768
MEMORY_RECALL_LIMIT = 5
MONTHLY_EMBEDDING_ATTEMPT_LIMIT = 10

# Sentiment/trigger extraction bounds (app/sentiment/, app/tools/sentiment_graph_tools.py).
# Emotion labels are a fixed taxonomy so the graph aggregates cleanly across
# entries; trigger labels are open-ended but canonicalized (see
# app/sentiment/vocabulary.py) so near-duplicate phrasing collapses to one
# label instead of forking the graph.
EMOTION_TAXONOMY: tuple[str, ...] = (
    "calm", "joy", "gratitude", "anxious", "sad",
    "frustrated", "tired", "motivated", "neutral",
)
MAX_EMOTIONS_PER_ENTRY = 3
MAX_TRIGGERS_PER_ENTRY = 3
MAX_TRIGGER_VOCABULARY_SIZE = 30
# Verified against concrete label pairs (app/sentiment/vocabulary.py's
# _label_similarity): 0.75 catches word-reordering ("stress at work" vs
# "work stress" -> 1.0) and plural/tense variants ("sleeping" vs "sleep" ->
# 0.769, "exercise" vs "exercising" -> 0.778) while still keeping distinct
# concepts apart ("work" vs "workout" -> 0.727, stays below threshold).
TRIGGER_LABEL_SIMILARITY_THRESHOLD = 0.75
MIN_MENTIONS_FOR_INSIGHT = 2

# Firestore document-id shaped identifiers (sessionId / idempotencyRequestId).
IDENTIFIER_PATTERN = r"^[A-Za-z0-9_-]{12,128}$"

# Shared cap on how many of a user's own interactions a single graph-building
# request reads from Firestore — used by both app/tools/graph_tools.py and
# app/tools/sentiment_graph_tools.py, which independently scan the same
# users/{uid}/interactions collection with different status filters. Kept
# here (not duplicated per-file) so both stay bounded the same way if this
# is ever relaxed.
MAX_GRAPH_NODES = 200


def month_key(now: datetime) -> str:
    """The `serviceLimits/monthly/months/{key}` document key, shared by
    every quota/capacity call site (app/tools/journal_tools.py,
    app/callbacks/guardrails.py) so the format can't drift between them."""
    return f"{now.year:04d}-{now.month:02d}"


def default_transactional() -> Callable:
    """The real `google.cloud.firestore.transactional` decorator, imported
    lazily so this module stays importable without the SDK installed (as in
    unit tests). Shared by every Firestore-transaction call site that needs
    an injectable escape hatch for tests — see app/tools/journal_tools.py's
    persist_completed_interaction and app/callbacks/guardrails.py's
    reserve_model_attempt, both of which accept a `transactional` override
    for exactly this reason: that real decorator drives actual GAPIC
    begin/commit/retry machinery a lightweight fake transaction can't
    satisfy, so tests pass `transactional=lambda fn: fn` instead.
    """
    from google.cloud import firestore

    return firestore.transactional


def project_id() -> str:
    return os.environ.get("GCLOUD_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "genai-academy-temp"


def is_test_bypass_enabled() -> bool:
    """Mirrors server.ts's isTestBypassEnabled: closed by construction in prod."""
    return os.environ.get("NODE_ENV") != "production" and os.environ.get("TEST_BYPASS_AUTH") == "true"


def cors_allowed_origins() -> list[str]:
    """The frontend (../src/) now calls the two GET /api/graph/* routes
    directly, cross-origin — a genuine new requirement, not present when
    this service only had to answer same-origin agent traffic. Defaults to
    "*" (every route here still requires a verified Firebase ID token, and
    none of them use cookies, so an open CORS policy doesn't itself grant
    access to anything); set CORS_ALLOWED_ORIGINS to a comma-separated list
    to restrict it for a real deployment.
    """
    raw = os.environ.get("CORS_ALLOWED_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]
