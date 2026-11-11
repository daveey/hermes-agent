"""Durable run-scoped failure commands for authoritative primary Boards. [daveey]

Queue under the caller's write transaction BEFORE changing mirrored task/run rows.
A command acknowledgement is not a mirrored state: only the Board feed finalizes
those rows and emits the public failure notice.
"""
from __future__ import annotations

import json
import sqlite3
import time

from hermes_cli import kanban_db_sync as sync


def deferred_crash(conn: sqlite3.Connection, dead) -> bool:
    """Charged primary crashes wait for authoritative release, not local mirror edits."""
    if sync.board_mode(conn) != "primary":
        return False
    if dead.host_reboot and dead.reboot_culprit:
        return True  # Culprit attribution outranks an owner loss during the same reboot.
    return not (
        dead.rate_limited or dead.owner_restart or dead.owner_unavailable
        or dead.host_reboot
    )


def queued(conn: sqlite3.Connection, run_id: int | None) -> bool:
    """Skip repeated observations/side effects while the authoritative feed lags."""
    if sync.board_mode(conn) != "primary":
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_failure_commands'").fetchone():
        return False
    return conn.execute("SELECT 1 FROM kanban_failure_commands WHERE run_uid=?",
                        (sync.run_uid(conn, run_id),)).fetchone() is not None


def queue(conn: sqlite3.Connection, task_id: str, run_id: int | None, *,
          outcome: str, error: str, **policy) -> bool:
    """True means Board authority owns this failure (including an already queued run).

    A missing portable identity on a primary board is an error, never permission
    to fall back to a competing local counter. Receipts survive outbox acks.
    """
    if sync.board_mode(conn) != "primary":
        return False
    uid = sync.run_uid(conn, run_id)
    if not uid:
        raise ValueError("primary failure release requires a portable run identity")
    row = conn.execute("SELECT task_id FROM task_runs WHERE id=?", (run_id,)).fetchone()
    if row[0] != task_id:
        raise ValueError("failure release run belongs to another task")
    if not conn.in_transaction:
        raise ValueError("failure release requires a write transaction")
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_failure_commands ("
                 "run_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
                 "op_id TEXT NOT NULL)")
    op_id = sync.uuid7()
    inserted = conn.execute("INSERT OR IGNORE INTO kanban_failure_commands VALUES (?, ?, ?)",
                            (uid, task_id, op_id)).rowcount
    if inserted:
        fields = {**policy, "run_uid": uid, "outcome": outcome, "error": error[:500]}
        conn.execute("INSERT INTO kanban_sync_outbox "
                     "(op_id,table_name,row_uid,op,fields,hlc,created_at) VALUES (?, 'tasks', ?, 'release', ?, '', ?)",
                     (op_id, task_id, json.dumps(fields), int(time.time())))
    return True
