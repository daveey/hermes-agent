"""Machine-local ended-run evidence, retained across foreign-copy eviction.

Run IDs in systemd scope names are local SQLite IDs, not Board run UIDs. Keep
that mapping in the same database, atomically with eviction; a Board replica's
numeric run ID is not interchangeable. This table deliberately has neither sync
capture nor a task foreign key. Absence is never evidence that a run ended.
"""
from __future__ import annotations

import sqlite3


_TABLE = "kanban_ended_run_tombstones"


def retain_ended_runs(conn: sqlite3.Connection, task_id: str) -> None:
    """Called inside eviction's write transaction, before deleting task_runs."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                 "task_id TEXT NOT NULL, run_id INTEGER NOT NULL, ended_at INTEGER NOT NULL, "
                 "worker_pid INTEGER, PRIMARY KEY (task_id, run_id))")
    conn.execute(f"INSERT OR IGNORE INTO {_TABLE} (task_id, run_id, ended_at, worker_pid) "
                 "SELECT task_id, id, ended_at, worker_pid FROM task_runs "
                 "WHERE task_id = ? AND ended_at IS NOT NULL", (task_id,))


def ended_run(conn: sqlite3.Connection, task_id: str, run_id: int):
    """Live rows win (including open runs); old databases need no read-side DDL."""
    row = conn.execute("SELECT ended_at, worker_pid FROM task_runs WHERE id = ? AND task_id = ?",
                       (run_id, task_id)).fetchone()
    if row is not None:
        return row
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone()
    if not exists:
        return None
    return conn.execute(f"SELECT ended_at, worker_pid FROM {_TABLE} WHERE run_id = ? AND task_id = ?",
                        (run_id, task_id)).fetchone()
