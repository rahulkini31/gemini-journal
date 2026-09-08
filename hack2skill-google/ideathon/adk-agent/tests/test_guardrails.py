"""Capacity reservation and App Check re-assertion, now a single check per
model attempt rather than a per-fallback-attempt loop (see
app/callbacks/guardrails.py's module docstring for why).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.callbacks.guardrails import (
    CapacityExhaustedError,
    assert_current_app_check,
    build_before_model_callback,
    reserve_model_attempt,
)
from app.config import MONTHLY_ATTEMPT_LIMIT
from app.security.app_check import AppCheckError
from tests.fakes import FakeFirestore


class _AlwaysFailsVerifier:
    def verify(self, token):
        raise AppCheckError("Valid App Check is required.")


class _AlwaysPassesVerifier:
    def verify(self, token):
        return None


def test_reserve_model_attempt_increments_the_monthly_counter():
    db = FakeFirestore()
    now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    reserve_model_attempt(db=db, now=now, transactional=lambda fn: fn)
    assert db.store["serviceLimits/monthly/months/2026-09"]["monthlyAttemptCount"] == 1
    reserve_model_attempt(db=db, now=now, transactional=lambda fn: fn)
    assert db.store["serviceLimits/monthly/months/2026-09"]["monthlyAttemptCount"] == 2


def test_reserve_model_attempt_raises_attempt_capacity_once_exhausted():
    db = FakeFirestore()
    now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    for _ in range(MONTHLY_ATTEMPT_LIMIT):
        reserve_model_attempt(db=db, now=now, transactional=lambda fn: fn)
    with pytest.raises(CapacityExhaustedError) as excinfo:
        reserve_model_attempt(db=db, now=now, transactional=lambda fn: fn)
    assert excinfo.value.code == "ATTEMPT_CAPACITY"


def test_assert_current_app_check_passes_through_when_no_verifier_configured():
    assert_current_app_check(app_check_verifier=None, app_check_token_provider=lambda: None)


def test_assert_current_app_check_raises_app_check_code_on_failure():
    with pytest.raises(CapacityExhaustedError) as excinfo:
        assert_current_app_check(
            app_check_verifier=_AlwaysFailsVerifier(),
            app_check_token_provider=lambda: "some-token",
        )
    assert excinfo.value.code == "APP_CHECK"


@pytest.mark.asyncio
async def test_before_model_callback_reserves_capacity_and_allows_the_call_to_proceed():
    db = FakeFirestore()
    now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    callback = build_before_model_callback(
        db=db, now=now, app_check_verifier=_AlwaysPassesVerifier(), app_check_token_provider=lambda: "token",
        transactional=lambda fn: fn,
    )
    result = await callback(callback_context=object(), llm_request=object())
    assert result is None  # None means "proceed" per ADK's before_model_callback contract
    assert db.store["serviceLimits/monthly/months/2026-09"]["monthlyAttemptCount"] == 1


@pytest.mark.asyncio
async def test_before_model_callback_blocks_the_call_when_app_check_fails():
    db = FakeFirestore()
    now = lambda: datetime(2026, 9, 1, tzinfo=timezone.utc)
    callback = build_before_model_callback(
        db=db, now=now, app_check_verifier=_AlwaysFailsVerifier(), app_check_token_provider=lambda: "token"
    )
    with pytest.raises(CapacityExhaustedError) as excinfo:
        await callback(callback_context=object(), llm_request=object())
    assert excinfo.value.code == "APP_CHECK"
    # App Check is asserted before capacity is reserved, so a failed check
    # must not consume a unit of the monthly attempt cap.
    assert "serviceLimits/monthly/months/2026-09" not in db.store
