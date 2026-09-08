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
from tests.fakes import FakeFirestore

UID = "user-a"
VALID_TOKEN = "valid-token-for-user-a"


class FakeAuthClient:
    def verify_id_token(self, token: str, check_revoked: bool = True) -> dict:
        if token != VALID_TOKEN:
            raise ValueError("invalid token")
        return {
            "uid": UID,
            "aud": "genai-academy-temp",
            "iss": "https://securetoken.google.com/genai-academy-temp",
            "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GCLOUD_PROJECT", "genai-academy-temp")
    db = FakeFirestore()
    app = create_app(db=db, auth_client=FakeAuthClient(), now=lambda: datetime.now(timezone.utc))
    return TestClient(app)


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
