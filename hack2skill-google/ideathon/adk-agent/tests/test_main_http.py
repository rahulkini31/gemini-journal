"""Smoke tests for the FastAPI wiring in app/main.py: auth/validation
boundaries, and the {"error": ...} response contract (matching
server.ts's safeError / src/App.tsx's safeErrorMessage) rather than
Starlette's default {"detail": ...} shape for HTTPException. Does not
exercise the full successful chat flow, which needs a real or stubbed
Gemini call — that path is covered structurally by test_fallback_ladder.py
(model sequencing) and the earlier manual Runner/session verification
recorded in this session, not duplicated here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import FakeAuthClient, FakeFirestore

UID = "user-a"
VALID_TOKEN = "valid-token-for-user-a"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GCLOUD_PROJECT", "genai-academy-temp")
    db = FakeFirestore()
    auth_client = FakeAuthClient().register(VALID_TOKEN, UID)
    app = create_app(db=db, auth_client=auth_client, now=lambda: datetime.now(timezone.utc))
    return TestClient(app)


@pytest.fixture
def client_and_db(monkeypatch):
    """Same wiring as `client`, but also hands back the underlying fake store
    so a test can seed prior state (e.g. a recent interaction) directly."""
    monkeypatch.setenv("GCLOUD_PROJECT", "genai-academy-temp")
    db = FakeFirestore()
    auth_client = FakeAuthClient().register(VALID_TOKEN, UID)
    app = create_app(db=db, auth_client=auth_client, now=lambda: datetime.now(timezone.utc))
    return TestClient(app), db


def test_quota_requires_authentication(client):
    response = client.get("/api/journal/quota")
    assert response.status_code == 401
    assert response.json() == {"error": "Authentication is required."}


def test_quota_rejects_an_invalid_token(client):
    response = client.get("/api/journal/quota", headers={"Authorization": "Bearer not-the-right-token"})
    assert response.status_code == 401
    assert response.json() == {"error": "Authentication is required."}


def test_quota_returns_camel_case_fields_for_a_valid_user(client):
    response = client.get("/api/journal/quota", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "completedInteractionCount", "completedInteractionLimit",
        "monthlyAttemptCount", "monthlyAttemptLimit", "cooldownSeconds",
    }
    assert body["completedInteractionCount"] == 0


def test_history_returns_an_empty_list_for_a_new_user(client):
    response = client.get("/api/journal/history", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 200
    assert response.json() == []


def test_interaction_rejects_a_body_over_4kib_before_touching_auth(client):
    oversized = "x" * 5000
    response = client.post("/api/journal/interaction", content=oversized.encode("utf-8"))
    assert response.status_code == 413
    assert response.json() == {"error": "Request body exceeds the 4 KiB limit."}


def test_interaction_rejects_malformed_json_before_touching_auth(client):
    response = client.post(
        "/api/journal/interaction",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Malformed JSON payload."}


def test_interaction_requires_app_check_even_with_a_valid_auth_token(client):
    response = client.post(
        "/api/journal/interaction",
        json={"prompt": "hello", "sessionId": "s" * 12, "idempotencyRequestId": "i" * 12},
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "Valid App Check is required."}


def test_interaction_rejects_an_unpermitted_field_once_authenticated(client, monkeypatch):
    monkeypatch.setenv("TEST_BYPASS_AUTH", "true")
    response = client.post(
        "/api/journal/interaction",
        json={"prompt": "hello", "sessionId": "s" * 12, "idempotencyRequestId": "i" * 12, "extra": "nope"},
        headers={
            "Authorization": f"Bearer {VALID_TOKEN}",
            "X-Firebase-AppCheck": "test-app-check-token",
        },
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid journal interaction payload."}


def test_interaction_rejects_within_cooldown_before_ever_touching_the_model(client_and_db, monkeypatch):
    """Found live against the real Featherless-backed model: without
    precheck_admission (app/tools/journal_tools.py), a cooldown-rejected
    request still ran the full chat + summary/sentiment model calls before
    persist_completed_interaction's own transactional check finally rejected
    it — spending two real, paid model attempts on a request that was always
    going to be refused. Seeds a just-now interaction directly in the fake
    store and confirms the HTTP layer rejects with 429 RATE_LIMIT — no
    FEATHERLESS_API_KEY is set, so if the model-building code path had run at
    all this would instead fail with 503 (see the sibling test above), not
    429; getting 429 here is the proof the model is never reached."""
    monkeypatch.setenv("TEST_BYPASS_AUTH", "true")
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    client, db = client_and_db
    db.store[f"users/{UID}"] = {"lastInteractionMs": int(datetime.now(timezone.utc).timestamp() * 1000)}

    response = client.post(
        "/api/journal/interaction",
        json={"prompt": "hello there", "sessionId": "s" * 12, "idempotencyRequestId": "i" * 12},
        headers={"Authorization": f"Bearer {VALID_TOKEN}", "X-Firebase-AppCheck": "test-app-check-token"},
    )
    assert response.status_code == 429
    assert response.json() == {"error": "Please wait one minute before starting another journal interaction."}


def test_interaction_with_a_valid_payload_fails_safely_without_a_configured_model_key(client, monkeypatch):
    """Proves the full auth + App Check + validation pipeline runs and fails
    closed with a safe error when there is no real Gemini credential —
    exactly the "no real credentials in tests" constraint this suite runs
    under, exercised as an actual behavior rather than just an absence."""
    monkeypatch.setenv("TEST_BYPASS_AUTH", "true")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    response = client.post(
        "/api/journal/interaction",
        json={"prompt": "hello there", "sessionId": "s" * 12, "idempotencyRequestId": "i" * 12},
        headers={
            "Authorization": f"Bearer {VALID_TOKEN}",
            "X-Firebase-AppCheck": "test-app-check-token",
        },
    )
    assert response.status_code == 503
    assert response.json() == {"error": "The reflection service is temporarily unavailable."}
