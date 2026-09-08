"""Shared loader for a user's own embedded journal interactions.

Both `app/tools/graph_tools.py` (the relationship graph) and
`app/memory/firestore_memory_service.py` (Memory Threads recall) scanned
`users/{uid}/interactions` for documents with `embeddingStatus ==
"completed"` and skipped any missing `automaticSessionSummary`/`embedding`
— the identical query and field contract, hand-rolled twice. Factored out
here (found by /simplify) so that contract lives in one place; each caller
still builds its own downstream shape (GraphNode's keywords, MemoryEntry's
timestamp) from the same loaded data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class EmbeddedInteraction:
    interaction_id: str
    summary: str
    embedding: list[float]
    user_prompt: str
    epoch_ms: int


def load_embedded_interactions(db: Any, uid: str, *, limit: Optional[int] = None) -> Iterable[EmbeddedInteraction]:
    query = db.collection(f"users/{uid}/interactions").where("embeddingStatus", "==", "completed")
    if limit is not None:
        query = query.limit(limit)
    for doc in query.stream():
        data = doc.to_dict() or {}
        summary = data.get("automaticSessionSummary")
        embedding = data.get("embedding")
        if not summary or not embedding:
            continue
        yield EmbeddedInteraction(
            interaction_id=doc.id,
            summary=summary,
            embedding=list(embedding),
            user_prompt=data.get("userPrompt") or "",
            epoch_ms=int((data.get("timestamps") or {}).get("epochMs") or 0),
        )
