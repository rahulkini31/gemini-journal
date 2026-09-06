"""Append-only decision log.

Refusals are logged with the same weight as trades. Over a 3.5-session judging
window most cycles will end in NO TRADE, and the record of *why* is the
substance of the submission.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> dict:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=str, sort_keys=True)
        # append + fsync: a crash must not lose the record of a live order
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for record in self.read_all():
            tally[record["event"]] = tally.get(record["event"], 0) + 1
        return tally
