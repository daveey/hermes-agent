"""Read-only native dispatcher lane queries, plus the reaper's live-owner check."""
import os
import sqlite3


def lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def still_current_worker(conn: sqlite3.Connection, row, pid: int) -> bool:
    """True when this ended run is still the card's live worker.

    A replica can close the run row while this host's worker is still the
    card's current one. Signalling then kills a live worker.
    """
    task = conn.execute(
        "SELECT status, worker_pid, current_run_id FROM tasks WHERE id = ?",
        (row["task_id"],),
    ).fetchone()
    return bool(
        task is not None and task["status"] == "running"
        and (task["worker_pid"] == pid or task["current_run_id"] == row["id"])
    )


def skip_reap(conn: sqlite3.Connection, row, pid: int, host_prefix: str) -> bool:
    """True when this run must not be signalled."""
    from hermes_cli import kanban_db as _kb

    return (
        pid == os.getpid()
        or not _kb._claim_is_local(row["claim_lock"], host_prefix)
        or still_current_worker(conn, row, pid)
    )
