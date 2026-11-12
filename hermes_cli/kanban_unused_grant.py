"""Distinguish a returned Board reservation from an owner execution. [daveey]"""
from __future__ import annotations

import sqlite3


def is_unused_grant(conn: sqlite3.Connection, run) -> bool:
    """Require a bare Board grant/release, with no contradictory execution trace."""
    from hermes_cli import kanban_db as kb

    metadata = kb._json_dict(run["metadata"])
    if (run["status"] != "released" or run["outcome"] != "released" or run["ended_at"] is None
            or set(metadata) != {"unused_grant"} or metadata.get("unused_grant") is not True):
        return False
    if any(run[key] is not None for key in ("worker_pid", "worker_started_at", "last_heartbeat_at", "claim_lock")):
        return False
    if run["summary"] or run["error"]:
        return False
    released = False
    for event in conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? AND run_id = ?",
                              (run["task_id"], run["id"])):
        payload = kb._json_dict(event["payload"])
        if event["kind"] == "claimed" and "lock" not in payload:
            continue
        if event["kind"] != "released" or payload.get("unused_grant") is not True:
            return False
        released = True
    return released


def latest_started_run(conn: sqlite3.Connection, task_id: str):
    """Latest run, ignoring only positively attested, unstarted unused grants.

    A missing receipt or any execution/failure evidence consumes the handoff.
    Keep run/event identity local: sync remaps their run ids together.
    """
    for run in conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC", (task_id,)):
        if not is_unused_grant(conn, run):
            return run
    return None
