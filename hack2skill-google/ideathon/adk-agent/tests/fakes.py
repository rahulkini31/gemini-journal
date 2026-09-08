"""Shared fakes for the test suite. No real Firestore/Firebase/Gemini calls
anywhere here — per docs/security-test-plan.md's "no real credential in
tests" rule, ported to this prototype."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


class FakeAuthClient:
    """A verify_id_token double supporting multiple users — one token maps
    to one uid, via `register`. Matches the real IdTokenVerifier protocol
    (app/security/firebase_auth.py): `verify_id_token(token, check_revoked)`."""

    def __init__(self, project_id: str = "genai-academy-temp"):
        self._project_id = project_id
        self._tokens: dict[str, str] = {}

    def register(self, token: str, uid: str) -> "FakeAuthClient":
        self._tokens[token] = uid
        return self

    def verify_id_token(self, token: str, check_revoked: bool = True) -> dict:
        uid = self._tokens.get(token)
        if uid is None:
            raise ValueError("invalid token")
        return {
            "uid": uid,
            "aud": self._project_id,
            "iss": f"https://securetoken.google.com/{self._project_id}",
            "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
        }


class FakeSnapshot:
    def __init__(self, data: dict | None):
        self._data = data
        self.exists = data is not None
        self.id = None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, store: dict, path: str):
        self._store = store
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def get(self, transaction=None):
        snap = FakeSnapshot(self._store.get(self.path))
        snap.id = self.id
        return snap

    def set(self, data: dict, merge: bool = False):
        existing = self._store.get(self.path) if merge else None
        merged = {**(existing or {}), **data}
        self._store[self.path] = merged

    def update(self, data: dict):
        existing = self._store.get(self.path) or {}
        self._store[self.path] = {**existing, **data}


class FakeQuery:
    def __init__(
        self,
        store: dict,
        prefix: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: tuple[str, str] | None = None,
        limit: int | None = None,
    ):
        self._store = store
        self._prefix = prefix
        self._filters = filters or []
        self._order_by = order_by
        self._limit = limit

    def where(self, field: str, op: str, value: Any) -> "FakeQuery":
        return FakeQuery(self._store, self._prefix, [*self._filters, (field, op, value)], self._order_by, self._limit)

    def order_by(self, field: str, direction: str = "ASCENDING") -> "FakeQuery":
        return FakeQuery(self._store, self._prefix, self._filters, (field, direction), self._limit)

    def limit(self, n: int) -> "FakeQuery":
        return FakeQuery(self._store, self._prefix, self._filters, self._order_by, n)

    def _matching_docs(self) -> list[tuple[str, dict]]:
        results = []
        for path, data in self._store.items():
            if not path.startswith(self._prefix):
                continue
            # Only direct children of the prefix (one path segment), so a
            # collection query never accidentally walks a subcollection.
            remainder = path[len(self._prefix):]
            if "/" in remainder:
                continue
            if all(_matches(data, field, op, value) for field, op, value in self._filters):
                results.append((path, data))
        if self._order_by is not None:
            field, direction = self._order_by
            results.sort(key=lambda pair: _nested_value(pair[1], field), reverse=(direction == "DESCENDING"))
        if self._limit is not None:
            results = results[: self._limit]
        return results

    def get(self, transaction=None) -> list[FakeSnapshot]:
        return list(self.stream())

    def stream(self) -> Iterable[FakeSnapshot]:
        for path, data in self._matching_docs():
            snap = FakeSnapshot(data)
            snap.id = path.rsplit("/", 1)[-1]
            yield snap


def _nested_value(data: dict, dotted_field: str) -> Any:
    value: Any = data
    for segment in dotted_field.split("."):
        value = (value or {}).get(segment) if isinstance(value, dict) else None
    # Sorting needs a total order even when the field is absent (e.g. a
    # legacy/partial fixture doc) — treat missing as the earliest value.
    return value if value is not None else datetime.min.replace(tzinfo=timezone.utc)


def _matches(data: dict, field: str, op: str, value: Any) -> bool:
    actual = data.get(field)
    if op == "==":
        return actual == value
    raise NotImplementedError(f"FakeQuery does not support operator {op!r}")


class FakeTransaction:
    """Duck-typed stand-in for google.cloud.firestore.Transaction: only
    `.get(ref)` (used via `ref.get(transaction=...)`) and `.set(ref, data,
    merge=...)` are exercised by app/tools/journal_tools.py's
    _admit_and_write, so that is all this implements."""

    def __init__(self, store: dict):
        self._store = store

    def set(self, ref: FakeDocRef, data: dict, merge: bool = False):
        ref.set(data, merge=merge)


class FakeFirestore:
    """A minimal in-memory Firestore double covering exactly the surface
    app/tools/journal_tools.py, app/memory/firestore_memory_service.py, and
    app/tools/graph_tools.py use: .document(path), .collection(path), and a
    transaction() that yields a FakeTransaction (only used to unit-test
    _admit_and_write directly, not through the real @transactional
    decorator — see journal_tools.py's docstring for why)."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def document(self, path: str) -> FakeDocRef:
        return FakeDocRef(self.store, path)

    def collection(self, path: str) -> FakeQuery:
        return FakeQuery(self.store, path.rstrip("/") + "/")

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self.store)


class FakeToolContext:
    def __init__(self, uid: str | None):
        self.state = {"uid": uid} if uid else {}


class FakeEmbeddingClient:
    """Deterministic, content-derived fake embedding — similar text yields a
    similar vector, without calling any real API."""

    def embed(self, text: str):
        # A tiny bag-of-characters vector is enough to make cosine similarity
        # behave sensibly for test fixtures without a real embedding model.
        vector = [0.0] * 26
        for char in text.lower():
            index = ord(char) - ord("a")
            if 0 <= index < 26:
                vector[index] += 1.0
        return vector
