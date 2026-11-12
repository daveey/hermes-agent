"""Run-scoped local failure receipts (primary boards use Board authority)."""
from __future__ import annotations

import sqlite3


def admit(conn: sqlite3.Connection, task_id: str, run_id: int | None) -> bool:
    """Claim accounting inside its transaction; never infer an ended run identity."""
    if run_id is None:
        return True  # Legacy callers without a run retain their old accounting.
    if conn.execute("SELECT 1 FROM task_runs WHERE id=? AND task_id=?", (run_id, task_id)).fetchone() is None:
        return False
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND id>?", (task_id, run_id)).fetchone():
        return False  # A delayed observer must not charge a replacement attempt.
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_local_failures ("
                 "run_id INTEGER PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE, "
                 "task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE)")
    return conn.execute("INSERT OR IGNORE INTO kanban_local_failures(run_id,task_id) VALUES (?,?)",
                        (run_id, task_id)).rowcount == 1


def finalized(conn: sqlite3.Connection, task_id: str, run_id: int | None, **payload) -> None:
    """Append the final notice atomically with the accounting receipt and counter."""
    if run_id is None:
        return
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    kb._append_event(conn, task_id, "failure_finalized", {
        **payload, "run_uid": sync.run_uid(conn, run_id) or f"local:{run_id}",
        "final_status": row["status"],
    }, run_id=run_id)
