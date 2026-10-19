"""Durable reservations fence unknown contexts; native sessions are never restored."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .journal import SqliteJournal
from .models import BrokerError, ServiceManifest, canonical_digest

MAX_RENEWAL_SNAPSHOT_SECONDS = 5.0


def owner_digest(owner: str) -> str:
    return canonical_digest({"frontend": owner})


class OwnershipJournal(SqliteJournal):
    def __init__(self, path: Path):
        super().__init__(path)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS contexts (
                service_id TEXT PRIMARY KEY, context_id TEXT UNIQUE NOT NULL,
                owner_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL,
                expires_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS resources (
                resource_key TEXT PRIMARY KEY, service_id TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS context_events (
                context_id TEXT PRIMARY KEY, service_id TEXT NOT NULL,
                reason TEXT NOT NULL, recorded_at REAL NOT NULL)""")

    def reserve(self, manifest: ServiceManifest, owner: str, context_id: str):
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO contexts VALUES (?, ?, ?, ?, 'opening', '', ?, ?)",
                    (
                        manifest.id,
                        context_id,
                        owner_digest(owner),
                        manifest.digest,
                        time.time() + manifest.lease_seconds,
                        time.time(),
                    ),
                )
                db.executemany(
                    "INSERT INTO resources VALUES (?, ?)",
                    [(key, manifest.id) for key in sorted(manifest.resource_keys)],
                )
        except sqlite3.IntegrityError:
            raise BrokerError(
                "resource already reserved; inspect service ownership/recovery"
            ) from None

    def activate(self, context_id: str, lease_seconds: float):
        with self.connect() as db:
            # Serialize the state check with retirement/recovery even when no
            # timestamp update is needed. Initial activation always commits.
            db.execute("BEGIN IMMEDIATE")
            record = db.execute(
                "SELECT state, expires_at, updated_at FROM contexts WHERE context_id=?",
                (context_id,),
            ).fetchone()
            if record is None or record[0] not in {"opening", "active"}:
                raise BrokerError("ownership reservation changed; operator recovery is required")
            stamp = time.time()
            interval = min(MAX_RENEWAL_SNAPSHOT_SECONDS, lease_seconds / 4)
            same_duration = abs((record[1] - record[2]) - lease_seconds) <= 1e-6
            if record[0] == "active" and same_duration and 0 <= stamp - record[2] < interval:
                # The broker renews its exact monotonic idle deadline on every
                # success. Journal expiry is an observational snapshot; orphaned
                # reservations remain blocked regardless of that timestamp.
                return
            db.execute(
                "UPDATE contexts SET state='active', expires_at=?, updated_at=? WHERE context_id=?",
                (stamp + lease_seconds, stamp, context_id),
            )

    def retiring(self, context_id: str):
        with self.connect() as db:
            updated = db.execute(
                "UPDATE contexts SET state='retiring', updated_at=? "
                "WHERE context_id=? AND state IN ('opening','active','retiring')",
                (time.time(), context_id),
            ).rowcount
            if updated != 1:
                raise BrokerError("context is lost; operator recovery is required")

    def lose(self, context_id: str, reason: str):
        with self.connect() as db:
            db.execute(
                "UPDATE contexts SET state='lost', reason=CASE WHEN state='lost' "
                "THEN reason ELSE ? END, updated_at=? WHERE context_id=?",
                (reason, time.time(), context_id),
            )

    def orphaned(self, live_contexts: set[str]):
        for entry in self.entries():
            if entry["context_id"] not in live_contexts:
                self.lose(entry["context_id"], "broker_restart")

    def entries(self) -> list[dict]:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM contexts ORDER BY service_id")]

    def get(self, service_id: str) -> dict | None:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM contexts WHERE service_id=?", (service_id,)).fetchone()
            return dict(row) if row else None

    def blockers(self, keys: set[str]) -> list[dict]:
        placeholders = ",".join("?" for _ in keys)
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT DISTINCT c.* FROM contexts c JOIN resources r "
                    "ON c.service_id=r.service_id WHERE r.resource_key IN (" + placeholders + ")",
                    sorted(keys),
                )
            ]

    def finish(self, service_id: str, context_id: str, reason: str, *, recovery=False):
        with self.connect() as db:
            row = db.execute(
                "SELECT state FROM contexts WHERE service_id=? AND context_id=?",
                (service_id, context_id),
            ).fetchone()
            if row is None:
                raise BrokerError("unknown or stale context_id; no reservation was cleared")
            if not recovery and row[0] != "retiring":
                raise BrokerError("context is lost; operator recovery is required")
            db.execute(
                "INSERT INTO context_events VALUES (?, ?, ?, ?)",
                (context_id, service_id, reason, time.time()),
            )
            db.execute("DELETE FROM resources WHERE service_id=?", (service_id,))
            db.execute(
                "DELETE FROM contexts WHERE service_id=? AND context_id=?", (service_id, context_id)
            )
