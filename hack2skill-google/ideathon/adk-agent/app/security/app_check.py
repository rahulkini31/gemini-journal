"""Firebase App Check token verification.

**Correction over the initial plan**: the plan flagged the Python Admin
SDK's App Check support as a likely gap, based on documentation search that
only surfaced `app_check.token()` (mint a token). Checked directly against
the actually-installed `firebase-admin` package (7.5.0) instead of trusting
that search: `firebase_admin.app_check.verify_token(token, app=None)` exists
and is the real verifier — so this wraps that directly rather than
hand-rolling JWKS/JWT verification. Ports server.ts:259-275's shape (require
the header, verify, never leak verifier internals on failure).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.config import is_test_bypass_enabled

TEST_BYPASS_TOKEN = "test-app-check-token"


class AppCheckError(Exception):
    """Raised for any App Check verification failure."""


class AppCheckTokenVerifier(Protocol):
    def verify_token(self, token: str, app: Any = None) -> dict: ...


@dataclass(frozen=True)
class AppCheckVerifier:
    verify_fn: AppCheckTokenVerifier
    firebase_app: Any = None

    def verify(self, token: str | None) -> None:
        if not token:
            raise AppCheckError("Valid App Check is required.")
        if is_test_bypass_enabled() and token == TEST_BYPASS_TOKEN:
            return
        try:
            self.verify_fn(token, app=self.firebase_app)
        except Exception as error:  # noqa: BLE001 - never leak verifier internals
            raise AppCheckError("Valid App Check is required.") from error


def build_default_verifier(*, firebase_app: Any = None) -> AppCheckVerifier:
    from firebase_admin import app_check

    return AppCheckVerifier(verify_fn=app_check.verify_token, firebase_app=firebase_app)
