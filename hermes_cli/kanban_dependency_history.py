"""Read-only dependency proof from the Board replica for parents this file lacks.

Two readers share one snapshot rule: historical-parent discharge after executor
partition eviction, and the completed review run of a parent that only ever ran
on another gateway.
"""
from __future__ import annotations

import sqlite3
import time


def _with_fresh_replica(conn, parked_at, read, default):
    """``read(replica)`` on one committed same-board snapshot pulled after the park.

    Missing, unreadable, stale, future-stamped or other-board replicas, and a
    local file with sync off, return ``default``: the caller's hold stays.
    """
    from hermes_cli.kanban_chat_links import _stale_after_s
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_foreign_card import open_replica

    try:
        local = conn.execute("SELECT board_id, mode FROM kanban_sync_state LIMIT 1").fetchone()
        if local is None or local["mode"] not in {"primary", "shadow"}:
            return default
        with open_replica(_main_db_file(conn)) as replica:
            if replica is None:
                return default
            # Pin stamp, task, run and events to one committed feed snapshot.
            replica.execute("BEGIN")
            state = replica.execute("SELECT board_id, pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
            if state is None or state["board_id"] != local["board_id"]:
                return default
            pulled = state["pulled_at"]
            if (type(pulled) not in (int, float) or pulled < parked_at
                    or not 0 <= time.time() - pulled <= _stale_after_s()):
                return default
            return read(replica)
    except (sqlite3.Error, OSError, ValueError):
        return default


def replica_historical_parent(conn, parent_id, parked_at):
    """Fresh same-board history, never a terminal status alone, can discharge history.

    This does not supply a review verdict or authorize a merge. The ordinary review
    checkpoint still binds the owner run, edges and PRs and is consumed on claim.
    Missing/old replicas and reopened parents retain the existing active-PR hold.
    """
    from hermes_cli.kanban_dependency_review import _historical_parent_history

    return _with_fresh_replica(
        conn, parked_at, lambda replica: _historical_parent_history(replica, parent_id, parked_at), False)


def replica_review_run(conn, parent_id, parked_at):
    """The parent's latest Board-replica run if it is a claimed completion that ended
    at or after the park, else None. Only for a parent with no local row.

    The caller applies the same verdict, exact-head and PR-scope rules as to a local
    run; this supplies Board data only, never a move- or comment-supplied verdict.
    """
    from hermes_cli import kanban_db as kb

    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (parent_id,)).fetchone() is not None:
        return None  # A local parent's own rows always decide.

    def read(replica):
        row = replica.execute(
            "SELECT r.*, EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id "
            "AND e.run_id = r.id AND e.kind = 'claimed') AS was_claimed "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE t.id = ? AND t.status = 'done' ORDER BY r.id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
        if (row is None or not row["was_claimed"] or row["outcome"] != "completed"
                or (kb._to_epoch(row["ended_at"]) or 0) < parked_at):
            return None
        return dict(row)

    return _with_fresh_replica(conn, parked_at, read, None)
