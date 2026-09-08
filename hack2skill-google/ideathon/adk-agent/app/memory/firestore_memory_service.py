"""Firestore-backed long-term memory, implementing ADK's `BaseMemoryService`.

Implements the Memory Threads design from
docs/ai-studio-memory-threads-prompt.md on top of the same
`users/{uid}/interactions/{interactionId}` documents the baseline app
already writes (server.ts) — no new database, no Vertex AI Agent Engine /
Memory Bank dependency, so it stays inside this project's free-tier cost
profile even though this prototype itself isn't gated by that profile.

Design invariants ported from the Memory Threads spec, not relaxed here:
- Embed only a completed interaction's automatic summary, never raw prompt
  text, tokens, or headers.
- One embedding per newly completed interaction; an embedding failure marks
  `embeddingStatus: "failed"` and must never fail or lose the completed
  interaction itself.
- Every read is scoped to the verified owner UID — there is no cross-user
  query path in this module.
- `search_memory` returns at most MEMORY_RECALL_LIMIT human-readable
  candidates and never includes the raw vector.

Verified against the actually-installed `google-adk` 2.8.0 (not just docs
search): `BaseMemoryService.add_session_to_memory(self, session) -> None`,
`search_memory(self, *, app_name, user_id, query) -> SearchMemoryResponse`
are both async, and results are real `google.adk.memory.MemoryEntry`
objects (`content: types.Content`, `id`, `author`, `timestamp`,
`custom_metadata`) — used directly here rather than a lookalike type, so
this service stays interoperable with the rest of ADK (e.g. its built-in
`load_memory` tool, even though this prototype uses its own recall tool).
`ADK's BaseMemoryService is an abstract interface intended for custom
persistent backends — the docs' own `DatabaseMemoryService` (SQL-backed)
example confirms this is a supported pattern, not a workaround.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

try:  # pragma: no cover - exercised via the real package when installed
    from google.adk.memory import BaseMemoryService
    from google.adk.memory.base_memory_service import SearchMemoryResponse
    from google.adk.memory.memory_entry import MemoryEntry
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - keeps this importable for unit tests without the SDK installed
    class BaseMemoryService:  # type: ignore[no-redef]
        pass

    SearchMemoryResponse = None  # type: ignore[assignment]
    MemoryEntry = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]

from app.config import MEMORY_EMBEDDING_DIMENSIONS, MEMORY_EMBEDDING_MODEL, MEMORY_RECALL_LIMIT
from app.models.embedding_client import EmbeddingClient
from app.similarity import cosine_similarity


class FirestoreMemoryService(BaseMemoryService):  # type: ignore[misc]
    def __init__(self, *, firestore_client: Any, embedding_client: EmbeddingClient):
        self._db = firestore_client
        self._embed = embedding_client

    # -- Writing memory --------------------------------------------------

    def embed_completed_interaction(self, *, uid: str, interaction_id: str, summary: str) -> None:
        """Called once, right after `save_journal_interaction` completes.

        Never raises past a caller that expects the baseline interaction to
        stay usable regardless of embedding outcome — failures are recorded
        on the document, not thrown.
        """
        doc_ref = self._db.document(f"users/{uid}/interactions/{interaction_id}")
        try:
            vector = list(self._embed.embed(summary))
            doc_ref.update({
                "embedding": vector,
                "embeddingStatus": "completed",
                "embeddingModel": MEMORY_EMBEDDING_MODEL,
                "embeddingDimensions": MEMORY_EMBEDDING_DIMENSIONS,
            })
        except Exception:  # noqa: BLE001 - embedding failure must not lose the interaction
            doc_ref.update({"embeddingStatus": "failed"})

    async def add_session_to_memory(self, session: Any) -> None:
        """Satisfies the BaseMemoryService contract for ADK-driven sessions.

        This app's own journal tool already persists+embeds interactions
        directly (see embed_completed_interaction above), so this is a
        best-effort no-op unless the session explicitly carries uid/
        interaction/summary state — it exists so a Runner wired with this
        service does not fail if it calls the generic ADK memory path.
        """
        state = getattr(session, "state", None) or {}
        uid = state.get("uid")
        interaction_id = state.get("last_interaction_id")
        summary = state.get("last_summary")
        if uid and interaction_id and summary:
            self.embed_completed_interaction(uid=uid, interaction_id=interaction_id, summary=summary)

    async def add_events_to_memory(self, **_kwargs: Any) -> None:
        """No-op: this app's memory is interaction-scoped, not event-scoped."""
        return None

    # -- Reading memory ---------------------------------------------------

    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> "SearchMemoryResponse":
        del app_name  # single-app prototype; kept for interface compatibility
        query_vector = list(self._embed.embed(query))
        entries = list(self._score_user_interactions(user_id, query_vector))
        entries.sort(key=lambda pair: pair[0], reverse=True)
        return SearchMemoryResponse(memories=[entry for _score, entry in entries[:MEMORY_RECALL_LIMIT]])

    def _score_user_interactions(
        self, uid: str, query_vector: Sequence[float]
    ) -> Iterable[tuple[float, "MemoryEntry"]]:
        docs = (
            self._db.collection(f"users/{uid}/interactions")
            .where("embeddingStatus", "==", "completed")
            .stream()
        )
        for doc in docs:
            data = doc.to_dict() or {}
            vector = data.get("embedding")
            summary = data.get("automaticSessionSummary")
            if not vector or not summary:
                continue
            score = cosine_similarity(query_vector, vector)
            epoch_ms = int((data.get("timestamps") or {}).get("epochMs") or 0)
            timestamp_iso = (
                datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat() if epoch_ms else None
            )
            entry = MemoryEntry(
                content=genai_types.Content(role="user", parts=[genai_types.Part(text=summary)]),
                id=doc.id,
                author="user",
                timestamp=timestamp_iso,
                custom_metadata={"score": score},
            )
            yield score, entry
