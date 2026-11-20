"""Retirement-only replay custody, in the owning profile's session store. [daveey]

Publishing is idempotent per delivery, claiming freezes a batch, and settlement is
fenced by the claim token. New deliveries never mutate an already claimed batch.
The transcript row can be committed in the SAME transaction via ``commit_message``.
Explicit Stop and successful-context watermarks survive backend replacement.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS automation_replay_items ("
    "id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL, due REAL NOT NULL, "
    "created_at REAL NOT NULL, persisted INTEGER NOT NULL, batch TEXT, status TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS automation_replay_pending ON automation_replay_items(owner,status)",
    "CREATE TABLE IF NOT EXISTS automation_replay_batches ("
    "id TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT, holder TEXT, payload TEXT NOT NULL, "
    "status TEXT NOT NULL, message_row_id INTEGER, message_uid TEXT, target TEXT, committed_at REAL)",
    "CREATE TABLE IF NOT EXISTS automation_replay_fences ("
    "owner TEXT PRIMARY KEY, stopped_at REAL NOT NULL DEFAULT 0, carried_through REAL NOT NULL DEFAULT 0)",
)


class Journal:
    """One exact profile; no fallback, profile discovery, or process-global home resolution."""

    def __init__(self, profile_home):
        self.path = Path(profile_home).expanduser().resolve() / "state.db"
        # A retry must never invent a profile/session store when its owner has vanished.
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=5.0)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    conn.execute(statement)
                yield conn

    def publish(self, owner: str, items: list[dict]) -> None:
        """Accept original delivery IDs, due dates and fields; repeats do not reset custody."""
        with self.transaction() as conn:
            fence = conn.execute("SELECT * FROM automation_replay_fences WHERE owner=?", (owner,)).fetchone()
            for item in items:
                created = item["created_at"]
                covered = fence and (created <= fence["stopped_at"] or
                                     (item["persisted"] and created <= fence["carried_through"]))
                payload = json.dumps(item, separators=(",", ":"))
                prior = conn.execute("SELECT owner,payload FROM automation_replay_items WHERE id=?",
                                     (item["id"],)).fetchone()
                if prior and (prior["owner"] != owner or prior["payload"] != payload):
                    raise ValueError("delivery identity already published with different owner or payload")
                conn.execute(
                    "INSERT OR IGNORE INTO automation_replay_items "
                    "(id,owner,payload,due,created_at,persisted,status) VALUES(?,?,?,?,?,?,?)",
                    (item["id"], owner, payload, item["due"], created,
                     bool(item["persisted"]), "cancelled" if covered else "queued"))

    def claim(self, owner: str, now: float, holder: str, holder_alive) -> dict | None:
        """Freeze one coalesced due batch, or recover the exact batch of a dead custodian.

        ``holder`` is a process-birth identity, not a reusable PID. No time expiry can
        steal a claim from a live dispatch; the caller supplies the OS liveness probe.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM automation_replay_batches WHERE owner=? AND status='pending'", (owner,)).fetchone()
            if row:
                if row["holder"] and holder_alive(row["holder"]):
                    return None
                batch = dict(row)
            else:
                rows = conn.execute(
                    "SELECT * FROM automation_replay_items WHERE owner=? AND status='queued' "
                    "ORDER BY created_at,id", (owner,)).fetchall()
                if not rows or max(row["due"] for row in rows) > now:
                    return None
                batch = {"id": uuid.uuid4().hex, "owner": owner,
                         "payload": json.dumps([json.loads(row["payload"]) for row in rows]),
                         "status": "pending", "message_row_id": None, "message_uid": None, "target": None}
                conn.execute(
                    "INSERT INTO automation_replay_batches(id,owner,payload,status) VALUES(?,?,?,'pending')",
                    (batch["id"], owner, batch["payload"]))
                conn.executemany("UPDATE automation_replay_items SET batch=?,status='batched' WHERE id=?",
                                 [(batch["id"], row["id"]) for row in rows])
            token = uuid.uuid4().hex
            conn.execute("UPDATE automation_replay_batches SET token=?,holder=? WHERE id=?",
                         (token, holder, batch["id"]))
            return {**batch, "token": token, "holder": holder}

    @staticmethod
    def _owned(conn, batch):
        return conn.execute(
            "SELECT * FROM automation_replay_batches WHERE id=? AND token=? AND status='pending'",
            (batch["id"], batch["token"])).fetchone()

    def commit_message(self, batch: dict, write_message, *, now: float | None = None) -> dict | None:
        """Commit/restage ONE transcript row under the claim fence.

        ``write_message(conn, uid)`` writes on this connection and returns (row_id,
        target). It must not commit. A crash rolls back both the row and its receipt.
        Reclaim after COMMIT returns the same receipt without invoking the writer.
        """
        with self.transaction() as conn:
            row = self._owned(conn, batch)
            if row is None:
                return None
            if row["message_row_id"] is None:
                uid = "automation-replay-" + row["id"]
                row_id, target = write_message(conn, uid)
                conn.execute("UPDATE automation_replay_batches SET message_row_id=?,message_uid=?,target=?,committed_at=? "
                             "WHERE id=? AND token=?", (row_id, uid, target, time.time() if now is None else now,
                                                      batch["id"], batch["token"]))
            return dict(conn.execute("SELECT * FROM automation_replay_batches WHERE id=?", (batch["id"],)).fetchone())

    def release(self, batch: dict) -> bool:
        """Leave a refused admission pending; a later pass may claim it again."""
        with self.transaction() as conn:
            return bool(conn.execute(
                "UPDATE automation_replay_batches SET token=NULL,holder=NULL WHERE id=? AND token=? "
                "AND status='pending'", (batch["id"], batch["token"])).rowcount)

    def settle(self, batch: dict) -> bool:
        with self.transaction() as conn:
            row = self._owned(conn, batch)
            if row is None or row["message_row_id"] is None:
                return False
            conn.execute("UPDATE automation_replay_batches SET status='dispatched',token=NULL,holder=NULL WHERE id=?",
                         (batch["id"],))
            conn.execute("UPDATE automation_replay_items SET status='dispatched' WHERE batch=?", (batch["id"],))
            return True

    def cancel(self, owner: str, through: float, *, stop: bool = False) -> None:
        """Fence late publication too; successful context never covers unpersisted deliveries.

        A partially covered batch is retired and its uncovered items are queued again.
        Its old claimant cannot commit or settle under the invalidated token.
        """
        column = "stopped_at" if stop else "carried_through"
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO automation_replay_fences(owner) VALUES(?)", (owner,))
            conn.execute(f"UPDATE automation_replay_fences SET {column}=MAX({column},?) WHERE owner=?",
                         (through, owner))
            # Once committed, the shared row is an indivisible context unit. A success
            # predating that row cannot split it and manufacture a duplicate survivor row.
            committed = conn.execute(
                "SELECT id,committed_at FROM automation_replay_batches WHERE owner=? "
                "AND status='pending' AND message_row_id IS NOT NULL", (owner,)).fetchall()
            for batch in committed:
                if not stop and batch["committed_at"] > through:
                    continue
                conn.execute("UPDATE automation_replay_batches SET status='cancelled',token=NULL,holder=NULL "
                             "WHERE id=?", (batch["id"],))
                conn.execute("UPDATE automation_replay_items SET status='cancelled' WHERE batch=?",
                             (batch["id"],))
            predicate = "" if stop else " AND persisted=1"
            predicate += (" AND (batch IS NULL OR batch IN (SELECT id FROM automation_replay_batches "
                          "WHERE message_row_id IS NULL))")
            rows = conn.execute("SELECT DISTINCT batch FROM automation_replay_items WHERE owner=? "
                                "AND created_at<=? AND status IN ('queued','batched')" + predicate,
                                (owner, through)).fetchall()
            conn.execute("UPDATE automation_replay_items SET status='cancelled' WHERE owner=? AND created_at<=? "
                         "AND status IN ('queued','batched')" + predicate, (owner, through))
            for row in rows:
                if row["batch"] is None:
                    continue
                conn.execute("UPDATE automation_replay_batches SET status='cancelled',token=NULL,holder=NULL "
                             "WHERE id=? AND status='pending'", (row["batch"],))
                conn.execute("UPDATE automation_replay_items SET batch=NULL,status='queued' "
                             "WHERE batch=? AND status='batched'", (row["batch"],))

    def owners(self) -> list[str]:
        with self.transaction() as conn:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT owner FROM automation_replay_items WHERE status IN ('queued','batched')")]
