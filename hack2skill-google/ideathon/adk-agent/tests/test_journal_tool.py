"""Ports SAVE-01/02 (idempotency), QUOTA-01/03 (capacity), and the cooldown
case from docs/security-test-plan.md against persist_completed_interaction
and get_quota_status. Uses FakeFirestore/FakeTransaction (tests/fakes.py) —
`transactional=lambda fn: fn` runs the exact same admission logic without
needing Firestore's real begin/commit/retry machinery (see
app/tools/journal_tools.py's persist_completed_interaction docstring)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import COMPLETED_INTERACTION_LIMIT, USER_COOLDOWN_SECONDS
from app.tools.journal_tools import (
    JournalToolError,
    build_journal_tools,
    persist_completed_interaction,
)
from tests.fakes import FakeFirestore, FakeToolContext

UID = "user-a"
SESSION = "session-abcdef123456"
REQUEST_1 = "request-abcdef123456"
REQUEST_2 = "request-fedcba654321"


def _save(db, now, *, session_id=SESSION, request_id=REQUEST_1, prompt="hello there"):
    return persist_completed_interaction(
        db=db,
        now=now,
        uid=UID,
        prompt=prompt,
        assistant_response="a supportive reply",
        automatic_session_summary="A short title",
        model_used="gemini-3.6-flash",
        summary_model_used="gemini-3.6-flash",
        session_id=session_id,
        idempotency_request_id=request_id,
        transactional=lambda fn: fn,
    )


def test_save_01_first_submission_persists_a_single_completed_interaction():
    db = FakeFirestore()
    now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    result = _save(db, now)
    assert result.duplicate is False
    assert result.status == "completed"
    assert result.turn_order == 1
    stored = db.store[f"users/{UID}/interactions/{REQUEST_1}"]
    assert stored["userPrompt"] == "hello there"
    assert stored["assistantResponse"] == "a supportive reply"
    assert stored["idempotencyRequestId"] == REQUEST_1


def test_save_02_retry_with_same_request_id_returns_original_without_duplicating():
    db = FakeFirestore()
    clock = {"t": datetime(2026, 9, 1, tzinfo=timezone.utc)}
    now = lambda: clock["t"]

    first = _save(db, now)
    clock["t"] += timedelta(seconds=5)
    second = _save(db, now)

    assert second.duplicate is True
    assert second.interaction_id == first.interaction_id
    assert second.assistant_response == first.assistant_response
    month_key = "2026-09"
    assert db.store[f"serviceLimits/monthly/months/{month_key}"]["completedInteractionCount"] == 1


def test_idempotency_mismatch_rejects_different_content_under_same_request_id():
    db = FakeFirestore()
    now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    _save(db, now, prompt="first content")
    with pytest.raises(JournalToolError) as excinfo:
        _save(db, now, prompt="different content entirely")
    assert excinfo.value.code == "IDEMPOTENCY_MISMATCH"


def test_rate_limit_blocks_a_second_fresh_interaction_within_the_cooldown():
    db = FakeFirestore()
    clock = {"t": datetime(2026, 9, 1, tzinfo=timezone.utc)}
    now = lambda: clock["t"]

    _save(db, now, request_id=REQUEST_1)
    clock["t"] += timedelta(seconds=USER_COOLDOWN_SECONDS - 1)
    with pytest.raises(JournalToolError) as excinfo:
        _save(db, now, request_id=REQUEST_2)
    assert excinfo.value.code == "RATE_LIMIT"


def test_rate_limit_clears_after_the_cooldown_elapses():
    db = FakeFirestore()
    clock = {"t": datetime(2026, 9, 1, tzinfo=timezone.utc)}
    now = lambda: clock["t"]

    _save(db, now, request_id=REQUEST_1)
    clock["t"] += timedelta(seconds=USER_COOLDOWN_SECONDS + 1)
    result = _save(db, now, request_id=REQUEST_2)
    assert result.duplicate is False
    assert result.turn_order == 2


def test_quota_01_completed_interaction_capacity_is_enforced_at_the_service_level():
    db = FakeFirestore()
    clock = {"t": datetime(2026, 9, 1, tzinfo=timezone.utc)}
    now = lambda: clock["t"]

    for index in range(COMPLETED_INTERACTION_LIMIT):
        clock["t"] += timedelta(seconds=USER_COOLDOWN_SECONDS + 1)
        _save(db, now, request_id=f"request-{index:012d}")

    clock["t"] += timedelta(seconds=USER_COOLDOWN_SECONDS + 1)
    with pytest.raises(JournalToolError) as excinfo:
        _save(db, now, request_id="request-overflow0001")
    assert excinfo.value.code == "INTERACTION_CAPACITY"


def test_get_quota_status_requires_a_verified_uid_from_session_state():
    db = FakeFirestore()
    _, get_quota_status = build_journal_tools(db=db, now=lambda: datetime.now(timezone.utc))
    with pytest.raises(JournalToolError) as excinfo:
        get_quota_status(FakeToolContext(uid=None))
    assert excinfo.value.code == "UNAUTHENTICATED"


def test_get_quota_status_reports_persisted_capacity():
    db = FakeFirestore()
    now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    _save(db, now)
    _, get_quota_status = build_journal_tools(db=db, now=now)
    status = get_quota_status(FakeToolContext(uid=UID))
    assert status["completed_interaction_count"] == 1
    assert status["completed_interaction_limit"] == COMPLETED_INTERACTION_LIMIT
