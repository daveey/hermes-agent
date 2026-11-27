"""Run terminal persistence; late-bound DB facade for compatibility."""
from __future__ import annotations

import sqlite3
import time
from typing import Optional


def _claim_is_live(trow) -> bool:
    """True when a ``running`` task's claim still protects a run: its spawned worker exists (PID + start-time
    fingerprint). TTL is not consulted: the process is the liveness authority (``reclaim_stale_tasks`` extends it)."""
    from hermes_cli import kanban_db as kb
    return bool(trow["status"] == "running" and trow["claim_lock"] is not None
                and trow["worker_pid"] and kb._worker_alive(trow["worker_pid"], trow["worker_started_at"]))

def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


def _end_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None, status: Optional[str] = None,
) -> Optional[int]:
    """Close the active run (``status`` defaults to ``outcome``) and clear
    ``current_run_id``; None when no run was active (never-claimed task).

    ``worker_pid`` / ``worker_started_at`` / ``claim_lock`` stay on the closed
    row: they are the only evidence left of the OS process once the task row
    is wiped, and :func:`kanban_db_dispatch.reap_terminal_workers` needs them
    to end a worker that survived its own terminal transition."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_origin_forward_run_end import require_settled
    require_settled(conn, task_id, _kb._current_run_id(conn, task_id), outcome)
    from hermes_cli.kanban_source_receipt import purge
    purge(conn, task_id)  # no pending source receipt outlives the run it was staged for
    now = int(time.time())
    run_id = _kb._current_run_id(conn, task_id)
    if run_id is None:
        return None
    from hermes_cli.kanban_workspace_publication import closed_metadata
    metadata = closed_metadata(conn, task_id, run_id, metadata)
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_expires = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (status or outcome, outcome, summary, error, _kb._json_or_null(metadata), now, run_id),
    )
    conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
    return run_id
