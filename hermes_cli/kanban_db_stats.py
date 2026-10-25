"""Board summary queries and explicit human-action counts."""
import sqlite3
import time


def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts and the oldest ``ready`` age (staleness signal)."""
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    from hermes_cli.kanban_db import _counts_by_assignee
    by_assignee = _counts_by_assignee(conn)

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    # Share the read-side SQL predicate with session counts; kinds alone are not asks.
    from hermes_cli.kanban_session_activity import BLOCKED_STATUSES, _status_filter

    match, params = _status_filter(conn, BLOCKED_STATUSES)
    waiting_ids = [r["id"] for r in conn.execute(
        f"SELECT t.id FROM tasks t WHERE {match} ORDER BY t.id", params)]

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "waiting_on_user": len(waiting_ids),
        "waiting_on_user_ids": waiting_ids,
        "now": now,
    }

