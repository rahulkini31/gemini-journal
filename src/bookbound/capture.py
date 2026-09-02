"""Immutable decision evidence bundles and deterministic offline replay.

The live market endpoints used by Bookbound do not offer historical option
snapshots.  A decision therefore has to preserve the *raw* response before any
filtering if it is to be auditable later.  This module is deliberately unaware
of the cycle orchestrator: its recording methods match the market ``raw_sink``
shape and sealing is a single atomic directory publication.

The hashes in this format are integrity checks, not signatures.  They detect
accidental corruption and uncoordinated edits; they do not protect against an
attacker who can rewrite both evidence and its manifest.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .config import Settings


CAPTURE_FORMAT = "bookbound.decision-capture.v1"
_CYCLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OMITTED_SETTINGS_FIELDS = frozenset(
    {"api_key", "secret_key", "featherless_key"}
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "credential",
    "authorization",
    "access_token",
    "refresh_token",
)
_PAYLOAD_PATHS = {
    "chain_pages": "market/chain-pages.json",
    "option_snapshot_batches": "market/option-snapshot-batches.json",
    "underlier_snapshot_batches": "market/underlier-snapshots.json",
    "context": "decision/context.json",
    "book": "portfolio/book.json",
    "working_orders": "portfolio/working-orders.json",
    "settings": "provenance/settings.json",
    "models": "provenance/models.json",
}


class CaptureIntegrityError(RuntimeError):
    """The replay bundle does not match its canonical manifest."""


@dataclass(frozen=True)
class CaptureReference:
    """Stable identity returned only after an atomic capture publication."""

    cycle_id: str
    as_of: datetime
    path: Path
    bundle_sha256: str


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    """Convert supported evidence values without introducing wall-clock data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured datetime values must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("captured mapping keys must be strings")
            converted[key] = _json_value(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_json_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported captured value type: {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    """Canonical UTF-8 JSON used for both persistence and hashing."""
    return (
        json.dumps(
            _json_value(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _looks_sensitive(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_sensitive(value: Any) -> Any:
    """Defensively redact secret-shaped keys in caller provenance."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _looks_sensitive(str(key))
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item) for item in value)
    return value


def settings_provenance(settings: Settings) -> dict[str, Any]:
    """Decision-affecting settings with all credential material omitted."""
    values = {
        field.name: copy.deepcopy(getattr(settings, field.name))
        for field in dataclasses.fields(settings)
        if field.name not in _OMITTED_SETTINGS_FIELDS
    }
    values["omitted_sensitive_fields"] = sorted(_OMITTED_SETTINGS_FIELDS)
    return _redact_sensitive(values)


def default_model_provenance(settings: Settings) -> dict[str, Any]:
    """Minimum model identity; callers should add prompt/revision hashes."""
    return {
        "proposer": {"model": settings.proposer_model},
        "adversary": {"model": settings.adversary_model},
    }


def _merge_model_provenance(
    settings: Settings, supplied: Mapping[str, Any] | None
) -> dict[str, Any]:
    merged = default_model_provenance(settings)
    for role, details in (supplied or {}).items():
        if isinstance(details, Mapping) and isinstance(merged.get(role), Mapping):
            merged[str(role)] = {**merged[str(role)], **copy.deepcopy(dict(details))}
        else:
            merged[str(role)] = copy.deepcopy(details)
    return _redact_sensitive(merged)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        path.chmod(0o555)
    root.chmod(0o555)


def _make_writable(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(0o755)
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


class DecisionCapture:
    """Mutable-in-memory recorder which can be sealed exactly once.

    Recording does no I/O.  ``seal`` canonicalizes all evidence in a private
    sibling directory and renames it into view only after every payload and the
    manifest have been flushed.  An existing cycle is never replaced.
    """

    def __init__(
        self,
        root: Path,
        *,
        cycle_id: str,
        as_of: datetime,
        settings: Settings,
        model_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        if not _CYCLE_ID_RE.fullmatch(cycle_id):
            raise ValueError(
                "cycle_id must contain only letters, digits, '.', '_' or '-'"
            )
        self.root = Path(root)
        self.cycle_id = cycle_id
        self.as_of = _aware_utc(as_of, field="as_of")
        self._chain_pages: list[dict[str, Any]] = []
        self._option_snapshot_batches: list[dict[str, Any]] = []
        self._underlier_snapshot_batches: list[dict[str, Any]] = []
        self._context: Any = None
        self._book: Any = None
        self._working_orders: Any = []
        self._context_recorded = False
        self._book_recorded = False
        self._orders_recorded = False
        self._settings = settings_provenance(settings)
        self._models = _merge_model_provenance(settings, model_provenance)
        self._sealed = False

    def _ensure_open(self) -> None:
        if self._sealed:
            raise RuntimeError("capture is already sealed")

    def record_chain_page(self, underlying: str, snapshots: dict) -> None:
        """Record one unfiltered chain page; directly usable as ``raw_sink``."""
        self._ensure_open()
        if not underlying:
            raise ValueError("underlying is required")
        self._chain_pages.append(
            {
                "sequence": len(self._chain_pages),
                "captured_at": self.as_of.isoformat(),
                "underlying": str(underlying),
                "snapshots": copy.deepcopy(snapshots),
            }
        )

    def record_option_snapshots(self, label: str, snapshots: dict) -> None:
        """Record one exact-symbol refresh; directly usable as ``raw_sink``."""
        self._ensure_open()
        if not label:
            raise ValueError("snapshot label is required")
        self._option_snapshot_batches.append(
            {
                "sequence": len(self._option_snapshot_batches),
                "captured_at": self.as_of.isoformat(),
                "label": str(label),
                "snapshots": copy.deepcopy(snapshots),
            }
        )

    def record_underlier_snapshot(
        self,
        symbol: str,
        snapshot: dict,
        *,
        phase: str | None = None,
    ) -> None:
        """Append a full stock snapshot, not merely its chosen price.

        The two positional arguments match the market ``raw_sink`` hook.  A
        caller that wraps the sink may also label the observation phase; the
        monotonic sequence remains authoritative when no label is supplied.
        """
        self._ensure_open()
        if not symbol:
            raise ValueError("underlier symbol is required")
        self._underlier_snapshot_batches.append(
            {
                "sequence": len(self._underlier_snapshot_batches),
                "captured_at": self.as_of.isoformat(),
                "symbol": str(symbol),
                "phase": str(phase) if phase is not None else None,
                "snapshot": copy.deepcopy(snapshot),
            }
        )

    def record_context(self, context: Any) -> None:
        self._ensure_open()
        if self._context_recorded:
            raise ValueError("context has already been recorded")
        self._context = copy.deepcopy(context)
        self._context_recorded = True

    def record_book(self, book: Any) -> None:
        self._ensure_open()
        if self._book_recorded:
            raise ValueError("book has already been recorded")
        self._book = copy.deepcopy(book)
        self._book_recorded = True

    def record_working_orders(self, orders: Any) -> None:
        self._ensure_open()
        if self._orders_recorded:
            raise ValueError("working orders have already been recorded")
        self._working_orders = copy.deepcopy(orders)
        self._orders_recorded = True

    def _payloads(self) -> dict[str, Any]:
        return {
            "chain_pages": self._chain_pages,
            "option_snapshot_batches": self._option_snapshot_batches,
            "underlier_snapshot_batches": self._underlier_snapshot_batches,
            "context": self._context,
            "book": self._book,
            "working_orders": self._working_orders,
            "settings": self._settings,
            "models": self._models,
        }

    def seal(self) -> CaptureReference:
        """Atomically publish and make the bundle read-only."""
        self._ensure_open()
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / self.cycle_id
        lock_path = self.root / f".{self.cycle_id}.lock"
        lock_descriptor: int | None = None
        temporary: Path | None = None
        try:
            lock_descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            if destination.exists():
                raise FileExistsError(f"capture already exists: {destination}")
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{self.cycle_id}.tmp-", dir=self.root)
            )

            file_records: list[dict[str, Any]] = []
            for key, relative in sorted(
                _PAYLOAD_PATHS.items(), key=lambda item: item[1]
            ):
                data = _canonical_json(self._payloads()[key])
                target = temporary / relative
                _write_bytes(target, data)
                file_records.append(
                    {
                        "path": relative,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )

            manifest_core = {
                "format": CAPTURE_FORMAT,
                "cycle_id": self.cycle_id,
                "as_of": self.as_of.isoformat(),
                "files": file_records,
            }
            bundle_sha256 = hashlib.sha256(
                _canonical_json(manifest_core)
            ).hexdigest()
            manifest = {**manifest_core, "bundle_sha256": bundle_sha256}
            _write_bytes(temporary / "manifest.json", _canonical_json(manifest))

            for directory in sorted(
                (path for path in temporary.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                _fsync_directory(directory)
            _fsync_directory(temporary)
            _make_read_only(temporary)
            os.rename(temporary, destination)
            temporary = None
            _fsync_directory(self.root)
            self._sealed = True
            return CaptureReference(
                cycle_id=self.cycle_id,
                as_of=self.as_of,
                path=destination,
                bundle_sha256=bundle_sha256,
            )
        finally:
            if temporary is not None and temporary.exists():
                _make_writable(temporary)
                shutil.rmtree(temporary)
            if lock_descriptor is not None:
                os.close(lock_descriptor)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass


class CaptureReplay:
    """Verified, network-free view of one sealed cycle bundle."""

    def __init__(
        self,
        *,
        path: Path,
        manifest: dict[str, Any],
        payloads: dict[str, Any],
    ) -> None:
        self.path = path
        self._manifest = manifest
        self._payloads = payloads

    @property
    def cycle_id(self) -> str:
        return str(self._manifest["cycle_id"])

    @property
    def as_of(self) -> datetime:
        parsed = datetime.fromisoformat(str(self._manifest["as_of"]))
        return _aware_utc(parsed, field="manifest as_of")

    @property
    def bundle_sha256(self) -> str:
        return str(self._manifest["bundle_sha256"])

    @property
    def manifest(self) -> dict[str, Any]:
        return copy.deepcopy(self._manifest)

    @property
    def chain_pages(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._payloads["chain_pages"])

    @property
    def option_snapshot_batches(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._payloads["option_snapshot_batches"])

    @property
    def underlier_snapshots(self) -> dict[str, Any]:
        """Latest observation per symbol, retained as a convenience view."""
        latest: dict[str, Any] = {}
        for batch in self._payloads["underlier_snapshot_batches"]:
            latest[str(batch["symbol"])] = batch["snapshot"]
        return copy.deepcopy(latest)

    @property
    def underlier_snapshot_batches(self) -> list[dict[str, Any]]:
        """Every observation in acquisition order, including repeated symbols."""
        return copy.deepcopy(self._payloads["underlier_snapshot_batches"])

    @property
    def context(self) -> Any:
        return copy.deepcopy(self._payloads["context"])

    @property
    def book(self) -> Any:
        return copy.deepcopy(self._payloads["book"])

    @property
    def working_orders(self) -> Any:
        return copy.deepcopy(self._payloads["working_orders"])

    @property
    def settings(self) -> dict[str, Any]:
        return copy.deepcopy(self._payloads["settings"])

    @property
    def models(self) -> dict[str, Any]:
        return copy.deepcopy(self._payloads["models"])

    def replay_chain_pages(self, sink: Callable[[str, dict], None]) -> None:
        """Feed raw pages through the same two-argument parser hook as live data."""
        for page in self.chain_pages:
            sink(page["underlying"], page["snapshots"])

    def replay_option_snapshots(self, sink: Callable[[str, dict], None]) -> None:
        for batch in self.option_snapshot_batches:
            sink(batch["label"], batch["snapshots"])

    def replay_underlier_snapshots(self, sink: Callable[[str, dict], None]) -> None:
        for batch in self.underlier_snapshot_batches:
            sink(batch["symbol"], batch["snapshot"])


def _regular_files(root: Path) -> set[str]:
    files: set[str] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            path = base / name
            if path.is_symlink():
                raise CaptureIntegrityError(f"symlink is not allowed: {path}")
        for name in file_names:
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise CaptureIntegrityError(f"non-regular file is not allowed: {path}")
            files.add(path.relative_to(root).as_posix())
    return files


def _safe_manifest_path(value: Any) -> str:
    if not isinstance(value, str):
        raise CaptureIntegrityError("manifest file path must be text")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise CaptureIntegrityError(f"unsafe manifest path: {value!r}")
    return value


def load_capture(path: Path) -> CaptureReplay:
    """Load a capture only after its manifest and every payload verify."""
    root = Path(path)
    manifest_path = root / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureIntegrityError(f"invalid capture manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CaptureIntegrityError("capture manifest must be an object")
    try:
        if manifest_bytes != _canonical_json(manifest):
            raise CaptureIntegrityError("manifest is not canonical JSON")
    except (TypeError, ValueError) as exc:
        raise CaptureIntegrityError(f"invalid capture manifest: {exc}") from exc
    if manifest.get("format") != CAPTURE_FORMAT:
        raise CaptureIntegrityError(
            f"unsupported capture format: {manifest.get('format')!r}"
        )
    if not _CYCLE_ID_RE.fullmatch(str(manifest.get("cycle_id") or "")):
        raise CaptureIntegrityError("invalid cycle_id in manifest")
    try:
        parsed_as_of = datetime.fromisoformat(str(manifest["as_of"]))
        _aware_utc(parsed_as_of, field="manifest as_of")
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureIntegrityError(f"invalid manifest as_of: {exc}") from exc

    file_records = manifest.get("files")
    if not isinstance(file_records, list):
        raise CaptureIntegrityError("manifest files must be a list")
    expected_paths = set(_PAYLOAD_PATHS.values())
    recorded_paths: set[str] = set()
    for record in file_records:
        if not isinstance(record, dict):
            raise CaptureIntegrityError("manifest file record must be an object")
        relative = _safe_manifest_path(record.get("path"))
        if relative in recorded_paths:
            raise CaptureIntegrityError(f"duplicate manifest file: {relative}")
        recorded_paths.add(relative)
    if recorded_paths != expected_paths:
        missing = sorted(expected_paths - recorded_paths)
        extra = sorted(recorded_paths - expected_paths)
        raise CaptureIntegrityError(
            f"capture file set mismatch; missing={missing}, unexpected={extra}"
        )

    actual_files = _regular_files(root)
    expected_disk_files = expected_paths | {"manifest.json"}
    if actual_files != expected_disk_files:
        missing = sorted(expected_disk_files - actual_files)
        extra = sorted(actual_files - expected_disk_files)
        detail = ""
        if missing:
            detail += f"missing file(s): {missing}"
        if extra:
            detail += ("; " if detail else "") + f"unexpected file(s): {extra}"
        raise CaptureIntegrityError(detail)

    payloads_by_path: dict[str, Any] = {}
    for record in file_records:
        relative = str(record["path"])
        data = (root / relative).read_bytes()
        if record.get("bytes") != len(data):
            raise CaptureIntegrityError(f"byte length mismatch for {relative}")
        digest = hashlib.sha256(data).hexdigest()
        if record.get("sha256") != digest:
            raise CaptureIntegrityError(f"hash mismatch for {relative}")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise CaptureIntegrityError(f"invalid JSON payload {relative}: {exc}") from exc
        try:
            canonical_payload = _canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise CaptureIntegrityError(
                f"invalid JSON value in {relative}: {exc}"
            ) from exc
        if data != canonical_payload:
            raise CaptureIntegrityError(f"non-canonical payload {relative}")
        payloads_by_path[relative] = payload

    manifest_core = {
        "format": manifest["format"],
        "cycle_id": manifest["cycle_id"],
        "as_of": manifest["as_of"],
        "files": file_records,
    }
    expected_bundle_hash = hashlib.sha256(_canonical_json(manifest_core)).hexdigest()
    if manifest.get("bundle_sha256") != expected_bundle_hash:
        raise CaptureIntegrityError("bundle hash mismatch")

    payloads = {
        key: payloads_by_path[relative]
        for key, relative in _PAYLOAD_PATHS.items()
    }
    return CaptureReplay(path=root, manifest=manifest, payloads=payloads)
