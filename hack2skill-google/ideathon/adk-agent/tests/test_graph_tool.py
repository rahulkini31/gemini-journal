"""Owner isolation, no-raw-vector-leakage, and transitive-relationship
correctness for the graph tool (app/tools/graph_tools.py) — the capability
beyond plain top-K similarity described in the plan's "Graph tool design"
section.

Uses hand-picked orthogonal-basis embedding vectors (see comments below) so
cosine similarities are exact, rather than a content-derived fake — the
scenario specifically needs "B is directly related to A, C is related to B
but NOT directly to A" to prove depth=1 vs depth=2 traversal actually
differs, and that needs precise control over which pairs cross the
similarity threshold.
"""

from __future__ import annotations

import pytest

from app.tools.graph_tools import build_graph_tool
from tests.fakes import FakeFirestore, FakeToolContext

USER_A = "user-a"
USER_B = "user-b"

# 5-dimensional one-hot basis, so any two vectors' cosine similarity is
# exactly computable by hand (see module docstring).
A_VEC = [1, 0, 0, 0, 0]
FILLER1_VEC = [0.6, 0.8, 0, 0, 0]  # 0.6 similar to A, otherwise isolated
FILLER2_VEC = [0.5, 0, 0.866, 0, 0]  # 0.5 similar to A, otherwise isolated
B_VEC = [0, 0, 0, 1, 0]  # 0 similar to A by embedding; connected via keywords instead
C_VEC = [0, 0, 0, 0.8, 0.6]  # 0.8 similar to B (>= threshold), 0 similar to A

A_SUMMARY = "sunrise hikes calm my mind"
B_SUMMARY = "sunrise hikes are the best part"  # shares "sunrise", "hikes" with A
C_SUMMARY = "quiet lakeside evenings restore energy"  # no keyword overlap with A/B
FILLER1_SUMMARY = "spreadsheet totals need review today"
FILLER2_SUMMARY = "budget forecast numbers look fine now"


class FixedEmbeddingClient:
    def __init__(self, by_text: dict[str, list[float]]):
        self._by_text = by_text

    def embed(self, text: str):
        return self._by_text[text]


def _seed(db: FakeFirestore, uid: str, doc_id: str, summary: str, vector: list[float]):
    db.document(f"users/{uid}/interactions/{doc_id}").set({
        "automaticSessionSummary": summary,
        "userPrompt": "",
        "embedding": vector,
        "embeddingStatus": "completed",
    })


@pytest.fixture
def seeded_db():
    db = FakeFirestore()
    _seed(db, USER_A, "a", A_SUMMARY, A_VEC)
    _seed(db, USER_A, "filler1", FILLER1_SUMMARY, FILLER1_VEC)
    _seed(db, USER_A, "filler2", FILLER2_SUMMARY, FILLER2_VEC)
    _seed(db, USER_A, "b", B_SUMMARY, B_VEC)
    _seed(db, USER_A, "c", C_SUMMARY, C_VEC)
    _seed(db, USER_B, "other-user-entry", "this belongs only to user b", [0, 0, 0, 0, 1])
    return db


@pytest.fixture
def embedding_client():
    return FixedEmbeddingClient({
        A_SUMMARY: A_VEC,
        B_SUMMARY: B_VEC,
        C_SUMMARY: C_VEC,
        FILLER1_SUMMARY: FILLER1_VEC,
        FILLER2_SUMMARY: FILLER2_VEC,
    })


@pytest.mark.asyncio
async def test_depth_1_returns_only_the_direct_neighbor(seeded_db, embedding_client):
    tool = build_graph_tool(db=seeded_db, embedding_client=embedding_client)
    result = await tool(A_SUMMARY, FakeToolContext(uid=USER_A), max_depth=1)

    related_ids = {entry["interaction_id"] for entry in result["related"]}
    assert "b" in related_ids
    assert "c" not in related_ids  # only reachable through b, which is 2 hops away


@pytest.mark.asyncio
async def test_depth_2_also_reaches_the_transitively_related_entry(seeded_db, embedding_client):
    tool = build_graph_tool(db=seeded_db, embedding_client=embedding_client)
    result = await tool(A_SUMMARY, FakeToolContext(uid=USER_A), max_depth=2)

    related_ids = {entry["interaction_id"] for entry in result["related"]}
    assert "b" in related_ids
    assert "c" in related_ids  # now reachable: a -> b -> c


@pytest.mark.asyncio
async def test_never_returns_another_users_entries(seeded_db, embedding_client):
    tool = build_graph_tool(db=seeded_db, embedding_client=embedding_client)
    result = await tool(A_SUMMARY, FakeToolContext(uid=USER_A), max_depth=2)

    related_ids = {entry["interaction_id"] for entry in result["related"]}
    assert "other-user-entry" not in related_ids


@pytest.mark.asyncio
async def test_never_exposes_raw_embedding_vectors(seeded_db, embedding_client):
    tool = build_graph_tool(db=seeded_db, embedding_client=embedding_client)
    result = await tool(A_SUMMARY, FakeToolContext(uid=USER_A), max_depth=2)

    for entry in result["related"]:
        assert set(entry.keys()) == {"interaction_id", "summary", "reason"}
