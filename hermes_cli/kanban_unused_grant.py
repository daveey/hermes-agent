"""Distinguish a returned Board reservation from an owner execution. [daveey]"""
from __future__ import annotations

import sqlite3


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)  # an Event/Run dataclass is not subscriptable


def _dict(raw) -> dict:
    from hermes_cli import kanban_db as kb
    return raw if isinstance(raw, dict) else kb._json_dict(raw)


# [daveey] The board's retained-run overlay shares this NULL-ness read set and the
# [daveey] P2-marked preconditions below. Extend both contracts before reading other bookkeeping.
UNUSED_GRANT_BOOKKEEPING = ("worker_pid", "worker_started_at", "last_heartbeat_at", "claim_lock")


def unused_grant_candidate(run) -> bool:
    """[daveey] P2-marked preconditions only; stable for a clean retained run."""
    if (_get(run, "status") not in (None, "released") or _get(run, "outcome") != "released"
            or _get(run, "ended_at") is None or _get(run, "summary") or _get(run, "error")):
        return False
    metadata = _dict(_get(run, "metadata"))
    return set(metadata) == {"unused_grant"} and metadata.get("unused_grant") is True


def unused_grant_run(run, events) -> bool:
    """Pure core of :func:`is_unused_grant`: ``run`` (row or dict) and ``events`` (that run's events, each with
    ``kind`` and ``payload`` as dict or JSON text) show a bare Board grant/release and nothing else."""
    if not unused_grant_candidate(run):
        return False
    if any(_get(run, key) is not None for key in UNUSED_GRANT_BOOKKEEPING):
        return False
    released = False
    for event in events:
        payload = _dict(_get(event, "payload"))
        if _get(event, "kind") == "claimed" and "lock" not in payload:
            continue
        if _get(event, "kind") != "released" or payload.get("unused_grant") is not True:
            return False
        released = True
    return released


def unused_grant_run_ids(runs, events) -> set:
    """Ids of ``runs`` that were unused grants, judged from the task's ``events`` (matched by ``run_id``)."""
    by_run: dict = {}
    for event in events:
        by_run.setdefault(_get(event, "run_id"), []).append(event)
    return {_get(run, "id") for run in runs
            if _get(run, "id") is not None and unused_grant_run(run, by_run.get(_get(run, "id"), []))}


def is_unused_grant(conn: sqlite3.Connection, run) -> bool:
    """Require a bare Board grant/release, with no contradictory execution trace."""
    events = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? AND run_id = ?",
                          (run["task_id"], run["id"])).fetchall()
    return unused_grant_run(run, events)


def latest_started_run(conn: sqlite3.Connection, task_id: str):
    """Latest run, ignoring only positively attested, unstarted unused grants.

    A missing receipt or any execution/failure evidence consumes the handoff.
    Keep run/event identity local: sync remaps their run ids together.
    """
    for run in conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC", (task_id,)):
        if not is_unused_grant(conn, run):
            return run
    return None
