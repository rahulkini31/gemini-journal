"""Step definitions for tests/features/graph_endpoints.feature.

Drives app/main.py::create_app via FastAPI's TestClient against
FakeFirestore/FakeAuthClient (tests/fakes.py) — no real Firestore,
Firebase, or model credentials, matching every other test in this
prototype (docs/security-test-plan.md's "no real credential in tests" rule).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.main import create_app
from tests.fakes import FakeAuthClient, FakeFirestore

scenarios("../graph_endpoints.feature")

TOKEN = "valid-token-for-primary-user"
UID = "user-primary"
OTHER_TOKEN = "valid-token-for-other-user"
OTHER_UID = "user-other"


@pytest.fixture
def db():
    return FakeFirestore()


@pytest.fixture
def client(db):
    auth_client = FakeAuthClient().register(TOKEN, UID).register(OTHER_TOKEN, OTHER_UID)
    app = create_app(db=db, auth_client=auth_client, now=lambda: datetime.now(timezone.utc))
    return TestClient(app)


@pytest.fixture
def response_holder():
    return {}


def _seed_relationship_entry(db: FakeFirestore, uid: str, doc_id: str, summary: str, embedding: list[float]):
    db.document(f"users/{uid}/interactions/{doc_id}").set({
        "automaticSessionSummary": summary,
        "userPrompt": "",
        "embedding": embedding,
        "embeddingStatus": "completed",
    })


def _seed_sentiment_entry(db: FakeFirestore, uid: str, doc_id: str, trigger: str, emotion: str, intensity: float = 0.7):
    db.document(f"users/{uid}/interactions/{doc_id}").set({
        "sentimentStatus": "completed",
        "sentimentAnalysis": {
            "emotions": [{"label": emotion, "intensity": intensity}],
            "triggers": [{"label": trigger, "phrase": f"a {trigger} example", "intensity": intensity}],
        },
    })


@given("a signed-in user with two related journal entries")
def seed_two_related_entries(db):
    # Two nearly-identical vectors, so they clear the similarity edge
    # threshold — mirrors tests/test_graph_tool.py's controlled fixture.
    _seed_relationship_entry(db, UID, "entry-a", "A calm morning walk", [1.0, 0.0, 0.0])
    _seed_relationship_entry(db, UID, "entry-b", "Another calm morning walk", [0.98, 0.02, 0.0])


@given("a signed-in user with no analyzed reflections")
def seed_nothing(db):
    del db  # deliberately empty


@given(parsers.parse('a signed-in user with a repeated "{trigger}" leading to "{emotion}" pattern'))
def seed_repeated_pattern(db, trigger, emotion):
    for index in range(2):
        _seed_sentiment_entry(db, UID, f"pattern-entry-{index}", trigger, emotion)


@given("a different user with their own unrelated journal entries")
def seed_other_user_entries(db):
    _seed_relationship_entry(db, OTHER_UID, "other-entry", "Something entirely different", [0.0, 1.0, 0.0])


@when("they request the relationship graph")
def request_relationship_graph(client, response_holder):
    response_holder["response"] = client.get("/api/graph/relationships", headers={"Authorization": f"Bearer {TOKEN}"})


@when("they request the emotional pattern graph")
def request_emotional_pattern_graph(client, response_holder):
    response_holder["response"] = client.get(
        "/api/graph/emotional-patterns", headers={"Authorization": f"Bearer {TOKEN}"},
    )


@when("an unauthenticated request is made for the relationship graph")
def request_relationship_graph_unauthenticated(client, response_holder):
    response_holder["response"] = client.get("/api/graph/relationships")


@then("the response includes both entries as nodes")
def assert_both_nodes_present(response_holder):
    body = response_holder["response"].json()
    node_ids = {node["id"] for node in body["nodes"]}
    assert {"entry-a", "entry-b"} <= node_ids


@then("an edge connects them")
def assert_edge_present(response_holder):
    body = response_holder["response"].json()
    endpoints = {frozenset((edge["source"], edge["target"])) for edge in body["edges"]}
    assert frozenset(("entry-a", "entry-b")) in endpoints


@then(parsers.parse('the response includes a pattern from "{trigger}" to "{emotion}"'))
def assert_pattern_present(response_holder, trigger, emotion):
    body = response_holder["response"].json()
    matching = [p for p in body["patterns"] if p["trigger"] == trigger and p["emotion"] == emotion]
    assert matching, f"no pattern {trigger!r} -> {emotion!r} in {body['patterns']!r}"
    response_holder["matched_pattern"] = matching[0]


@then(parsers.parse("the pattern's mention count is at least {minimum:d}"))
def assert_mention_count_at_least(response_holder, minimum):
    assert response_holder["matched_pattern"]["mention_count"] >= minimum


@then("the response is a successful empty graph")
def assert_empty_graph(response_holder):
    response = response_holder["response"]
    assert response.status_code == 200
    body = response.json()
    assert body["nodes"] == []
    assert body["edges"] == []


@then(parsers.parse("the request is rejected with {status:d}"))
def assert_rejected_with_status(response_holder, status):
    assert response_holder["response"].status_code == status


@then("the response never includes the other user's entries")
def assert_no_cross_user_leakage(response_holder):
    body = response_holder["response"].json()
    node_ids = {node["id"] for node in body["nodes"]}
    assert "other-entry" not in node_ids
