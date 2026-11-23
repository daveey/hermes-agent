"""Host-local custody of neutral run-end releases, independent of row sync. [daveey]

The ordinary status push has 120s to close the Board lease without a ready window.
If it does not, the dispatcher retries the same exact-run release used by the
already-stalled fast path. This does not override server expiry or repair a
charged run. A failed call stays durable; a successor run is fenced by the Board.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)
TABLE = "kanban_run_end_retries"
GRACE_SECONDS = 120
BATCH_SIZE = 16


def note(conn: sqlite3.Connection, task: str, uid: str) -> None:
    """Called in the run-ending transaction, so rollback drops the obligation too."""
    state = sync.sync_state(conn)
    assert state is not None  # callers require a configured primary board
    conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (run_uid TEXT PRIMARY KEY, "
                 "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
                 "gateway TEXT NOT NULL, board TEXT NOT NULL, due_at INTEGER NOT NULL)")
    conn.execute(f"INSERT OR IGNORE INTO {TABLE} VALUES (?, ?, ?, ?, ?)",
                 (uid, task, state["gateway_id"], state["board_id"], int(time.time()) + GRACE_SECONDS))


def drain(conn: sqlite3.Connection) -> None:
    """Bounded dispatcher maintenance, including when no card remains running."""
    if sync.board_mode(conn) != "primary" or conn.in_transaction:
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return
    now = int(time.time())
    state = sync.sync_state(conn)
    assert state is not None  # callers require a configured primary board
    rows = conn.execute(f"SELECT * FROM {TABLE} WHERE due_at<=? AND gateway=? AND board=? "
                        "ORDER BY due_at,run_uid LIMIT ?",
                        (now, state["gateway_id"], state["board_id"], BATCH_SIZE)).fetchall()
    for row in rows:
        send(conn, row["task_id"], row["run_uid"])


def send(conn: sqlite3.Connection, task: str, uid: str) -> None:
    """Only a committed closed run and its original gateway may send this release."""
    row = conn.execute(f"SELECT q.*,r.ended_at FROM {TABLE} q JOIN task_runs r ON r.uid=q.run_uid "
                       "AND r.task_id=q.task_id WHERE q.run_uid=? AND q.task_id=?", (uid, task)).fetchone()
    if row is None or row["ended_at"] is None:
        return  # a rolled-back savepoint did not end the run
    state = sync.sync_state(conn)
    assert state is not None  # callers require a configured primary board
    if state["gateway_id"] != row["gateway"] or state["board_id"] != row["board"]:
        return  # custody is host-local, not permission to act through a new principal
    try:
        sync._client().release(board_id=row["board"], task_uid=task, run_uid=uid, gateway_id=row["gateway"])
    except sync._CLIENT_ERRORS as exc:
        _log.info("kanban run-end release for %s/%s deferred: %s", task, uid, exc)
        from hermes_cli.kanban_db_connect import write_txn
        with write_txn(conn):
            conn.execute(f"UPDATE {TABLE} SET due_at=? WHERE run_uid=?", (int(time.time()) + GRACE_SECONDS, uid))
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute(f"DELETE FROM {TABLE} WHERE run_uid=?", (uid,))
