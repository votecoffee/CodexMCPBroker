"""Durable write claims prevent broker retries from dispatching a write twice."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import BrokerError


def now() -> str:
    return datetime.now(UTC).isoformat()


class WriteJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                tool_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            # sqlite3's transaction context manager commits/rolls back but does
            # not close the connection. Do not depend on cyclic GC for Windows handles.
            db.close()

    def claim(self, request_id: str, fingerprint: str, tool_id: str) -> None:
        if not 8 <= len(request_id) <= 128:
            raise BrokerError("request_id must contain 8 to 128 characters")
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, fingerprint, tool_id, "in_progress", now(), now()),
                )
        except sqlite3.IntegrityError:
            entry = self.status(request_id)
            if entry["fingerprint"] != fingerprint:
                raise BrokerError("request_id was already used for a different write") from None
            raise BrokerError(
                f"write already claimed ({entry['status']}); inspect get_request_status; "
                "do not retry with a new ID until the app's actual state is checked"
            ) from None

    def finish(self, request_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET status=?, updated_at=? WHERE request_id=?",
                (status, now(), request_id),
            )

    def status(self, request_id: str) -> dict:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise BrokerError("unknown request_id")
        return dict(row)
