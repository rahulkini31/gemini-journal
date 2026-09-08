"""Ports DB-05/DB-06 (owner isolation, no raw vectors) and the "embedding
failure must not lose the interaction" invariant from
docs/ai-studio-memory-threads-prompt.md, against FirestoreMemoryService.

Recall being opt-in (only the model choosing to call the tool triggers a
search) is an agent-instruction property, not something a unit test over the
memory service can prove — see app/agent.py's SYSTEM_INSTRUCTION and the
README's "what isn't unit-tested" section.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import MEMORY_RECALL_LIMIT
from app.memory.firestore_memory_service import FirestoreMemoryService
from tests.fakes import FakeEmbeddingClient, FakeFirestore

USER_A = "user-a"
USER_B = "user-b"


def _seed_interaction(db: FakeFirestore, uid: str, interaction_id: str, summary: str, embedding):
    db.document(f"users/{uid}/interactions/{interaction_id}").set({
        "automaticSessionSummary": summary,
        "embedding": embedding,
        "embeddingStatus": "completed",
        "timestamps": {"epochMs": int(datetime.now(timezone.utc).timestamp() * 1000)},
    })


@pytest.mark.asyncio
async def test_db_05_search_memory_only_returns_the_calling_users_own_entries():
    db = FakeFirestore()
    embed = FakeEmbeddingClient()
    _seed_interaction(db, USER_A, "a1", "focused walk in the park", embed.embed("focused walk in the park"))
    _seed_interaction(db, USER_B, "b1", "this belongs only to user b", embed.embed("this belongs only to user b"))

    service = FirestoreMemoryService(firestore_client=db, embedding_client=embed)
    response = await service.search_memory(app_name="app", user_id=USER_A, query="walk")

    assert len(response.memories) == 1
    assert response.memories[0].id == "a1"


@pytest.mark.asyncio
async def test_search_memory_never_exposes_a_raw_vector_field():
    db = FakeFirestore()
    embed = FakeEmbeddingClient()
    _seed_interaction(db, USER_A, "a1", "a quiet morning reflection", embed.embed("a quiet morning reflection"))

    service = FirestoreMemoryService(firestore_client=db, embedding_client=embed)
    response = await service.search_memory(app_name="app", user_id=USER_A, query="morning")

    entry = response.memories[0]
    # MemoryEntry's declared fields are content/custom_metadata/id/author/timestamp —
    # assert directly that no vector sneaks into custom_metadata either.
    assert "embedding" not in entry.custom_metadata
    assert "vector" not in entry.custom_metadata


@pytest.mark.asyncio
async def test_search_memory_caps_results_at_the_configured_limit():
    db = FakeFirestore()
    embed = FakeEmbeddingClient()
    for index in range(MEMORY_RECALL_LIMIT + 3):
        text = f"reflection number {index} about focus and calm"
        _seed_interaction(db, USER_A, f"a{index}", text, embed.embed(text))

    service = FirestoreMemoryService(firestore_client=db, embedding_client=embed)
    response = await service.search_memory(app_name="app", user_id=USER_A, query="focus and calm")

    assert len(response.memories) == MEMORY_RECALL_LIMIT


def test_embedding_failure_marks_status_failed_without_losing_the_interaction():
    db = FakeFirestore()
    db.document(f"users/{USER_A}/interactions/a1").set({
        "automaticSessionSummary": "already completed and saved",
        "status": "completed",
    })

    class FailingEmbeddingClient:
        def embed(self, text: str):
            raise RuntimeError("embedding backend unavailable")

    service = FirestoreMemoryService(firestore_client=db, embedding_client=FailingEmbeddingClient())
    service.embed_completed_interaction(uid=USER_A, interaction_id="a1", summary="already completed and saved")

    stored = db.store[f"users/{USER_A}/interactions/a1"]
    assert stored["embeddingStatus"] == "failed"
    # The baseline interaction itself is untouched and still marked completed.
    assert stored["status"] == "completed"
    assert stored["automaticSessionSummary"] == "already completed and saved"
