"""Read-only native dispatcher lane queries, plus the reaper's live-owner check."""
import os
import sqlite3
from hermes_cli.kanban_priority import PRIORITY_CAP


# Compute rather than store inheritance: edits, imported edges and unlinking take
# effect on the next read, without overwriting user priorities or foreign-owned
# rows. A parent is the prerequisite; child_id is the card waiting on it.
# Include open waiters before they park, and traverse the entire active chain.
# UNION (not UNION ALL) terminates even if imported edges contain a cycle.
# Expressions require an unaliased outer ``tasks`` table. [daveey]
_OPEN_WAITER = "w.status NOT IN ('done', 'archived')"
_WAITERS = (
    "FROM task_links wl JOIN tasks w ON w.id = wl.child_id "
    f"WHERE wl.parent_id = tasks.id AND {_OPEN_WAITER}"
)
EFFECTIVE_PRIORITY = (
    "(WITH RECURSIVE waiting(id) AS ("
    f"SELECT w.id {_WAITERS} UNION "
    "SELECT w.id FROM waiting a JOIN task_links wl ON wl.parent_id = a.id "
    f"JOIN tasks w ON w.id = wl.child_id WHERE {_OPEN_WAITER}"
    f") SELECT MIN({PRIORITY_CAP}, MAX(COALESCE(tasks.priority, 0), "
    "COALESCE(MAX(w.priority), COALESCE(tasks.priority, 0)))) "
    "FROM waiting a JOIN tasks w ON w.id = a.id)"
)
DISPATCH_ORDER = (
    f"{EFFECTIVE_PRIORITY} DESC, "
    f"EXISTS (SELECT 1 {_WAITERS}) DESC, tasks.created_at ASC"
)


def lane_rows(conn: sqlite3.Connection, status: str, *, marked_first: bool = False) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order; optional pool ordering keeps priority ties per class."""
    rows = conn.execute(
        "SELECT id, assignee, admission_class FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        f"ORDER BY {DISPATCH_ORDER}"
    ).fetchall()
    from hermes_cli.kanban_containment_connections import drop_held
    from hermes_cli.kanban_governors import dropped  # the rows each filter removes are charged to its governor
    rows = dropped(rows, drop_held(conn, rows), "containment")
    from hermes_cli.kanban_dispatch_priority import order_with_remote_waiters
    from hermes_cli.kanban_lanes import drop_paused
    from hermes_cli.kanban_lane_workers import drop_full
    rows = dropped(rows, drop_paused(conn, rows), "lane_paused")
    rows = order_with_remote_waiters(conn, dropped(rows, drop_full(conn, rows), "lane_max_workers"))
    if marked_first:  # replica donation reorders within a class, never across the pool partition
        rows.sort(key=lambda row: row["admission_class"] not in ("capacity", "urgent"))
    return rows


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
