"""Read-only native dispatcher lane queries, plus the reaper's live-owner check."""
import os
import sqlite3


# A card another card waits on (a ``todo`` child) is dispatched first among its effective priority, and that
# priority is the highest of its own and its waiting children's. Before 11-09 the lane was priority then age only:
# a p0 parent of a waiting rollout sat ready 2 h behind 61 older p0 leaves (kanban-447c98343f). The stored
# priority never changes; explicit priority still wins. Rows of ``tasks`` (unaliased) only. [daveey]
_WAITERS = "FROM task_links wl JOIN tasks w ON w.id = wl.child_id WHERE wl.parent_id = tasks.id AND w.status = 'todo'"
DISPATCH_ORDER = (
    f"MAX(COALESCE(tasks.priority, 0), COALESCE((SELECT MAX(COALESCE(w.priority, 0)) {_WAITERS}), 0)) DESC, "
    f"EXISTS (SELECT 1 {_WAITERS}) DESC, tasks.created_at ASC"
)


def lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order (:data:`DISPATCH_ORDER`)."""
    return conn.execute(
        "SELECT id, assignee FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        f"ORDER BY {DISPATCH_ORDER}"
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
