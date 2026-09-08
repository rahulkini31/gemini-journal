"""Firebase ID token verification.

Ports `verifyAuth` from server.ts:238-257. `firebase_admin.auth.verify_id_token`
already checks signature/issuer/audience/expiry, but this project's threat
model (docs/ai-studio-threat-model.md, Zone 1) requires the server to
*independently* re-check uid/aud/iss/exp rather than trust the SDK's decoded
claims blindly, so that is repeated here exactly as it is in the Node
baseline — defense in depth, not redundancy for its own sake.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from app.config import is_test_bypass_enabled


class AuthError(Exception):
    """Raised for any authentication failure. Never carries verifier internals."""


class IdTokenVerifier(Protocol):
    def verify_id_token(self, token: str, check_revoked: bool = True) -> dict: ...


@dataclass(frozen=True)
class VerifiedUser:
    uid: str
    claims: dict


TEST_BYPASS_TOKEN = "test-id-token"
TEST_BYPASS_UID = "test-user"


def verify_authorization_header(
    header: str | None,
    *,
    auth_client: IdTokenVerifier,
    project_id: str,
    now: Callable[[], float] = time.time,
) -> VerifiedUser:
    """Raises AuthError on any failure. Never exposes the verifier's own error text."""
    if not header or not header.startswith("Bearer ") or len(header) <= len("Bearer "):
        raise AuthError("Authentication is required.")
    token = header[len("Bearer "):]

    if is_test_bypass_enabled() and token == TEST_BYPASS_TOKEN:
        return VerifiedUser(uid=TEST_BYPASS_UID, claims={
            "uid": TEST_BYPASS_UID,
            "aud": project_id,
            "iss": f"https://securetoken.google.com/{project_id}",
            "exp": int(now()) + 60,
        })

    try:
        decoded = auth_client.verify_id_token(token, check_revoked=True)
    except Exception as error:  # noqa: BLE001 - deliberately opaque to the caller
        raise AuthError("Authentication is required.") from error

    uid = decoded.get("uid") or decoded.get("user_id")
    expected_issuer = f"https://securetoken.google.com/{project_id}"
    exp = decoded.get("exp")
    if not uid or decoded.get("aud") != project_id or decoded.get("iss") != expected_issuer or not exp or exp <= now():
        raise AuthError("Authentication is required.")

    return VerifiedUser(uid=uid, claims=decoded)
