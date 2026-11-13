"""Whether a ``running`` card's worker can still read a new comment. [daveey]

Incident (2026-11-13, hermes-worker-6, t_01a1d563324d7e8e run 11569): the continued worker ended its turn rc=0 at
15:55 and both pids were dead, but the run row still read ``running``. A ``hermes kanban comment`` at 16:05 answered
"The card has a live worker run: it gets this note at its next model call". No process would ever make that call.
The comment paths read only the task's status (``kanban_db.live_run_id``); this module asks about the worker.
"""
from __future__ import annotations

import json
import sqlite3

__all__ = ["EXITED_NOTE", "comment_live_fields", "worker_exited"]

EXITED_NOTE = ("the card's worker has exited (its run is not closed yet): no live run will read this note; "
               "it waits for the card's next run")


def _latest(conn: sqlite3.Connection, task_id: str, run_id: int, kind: str):
    row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND run_id=? AND kind=? "
                       "ORDER BY id DESC LIMIT 1", (task_id, run_id, kind)).fetchone()
    if row is None:
        return None, {}
    try:
        payload = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return int(row[0]), payload if isinstance(payload, dict) else {}


def _pids_dead(conn: sqlite3.Connection, task_id: str, run_id: int) -> bool:
    """Every recorded process of the run is gone, judged only for a claim this host issued."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _worker_alive
    row = conn.execute("SELECT claim_lock, worker_pid, worker_started_at FROM tasks WHERE id=?",
                       (task_id,)).fetchone()
    if row is None or not row[1] or not kb._claim_is_local(row[0] or "", kb._host_prefix()):
        return False  # no pid yet (launch window) or another host's pid: no proof
    if _worker_alive(row[1], row[2]):
        return False
    for (payload,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                                   "AND kind='worker_bound'", (task_id, run_id)):
        try:
            bound = json.loads(payload or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(bound, dict) or (bound.get("pid") and _worker_alive(bound["pid"], bound.get("started_at"))):
            return False
    return True


def worker_exited(conn: sqlite3.Connection, task_id: str, run_id: int) -> bool:
    """True when the live run's worker can no longer read a comment: its final result is booked after its last
    binding and admission (a continuable restart checkpoint is not final: the next owner continues the turn), or
    every process this host recorded for the run is dead. Unreadable or missing evidence is False."""
    try:
        result_id, result = _latest(conn, task_id, run_id, "worker_result")
        if result_id is not None and not result.get("continuable"):
            later = [i for i in (_latest(conn, task_id, run_id, k)[0] for k in ("worker_bound", "owner_admitted"))
                     if i is not None]
            if all(result_id > i for i in later):
                return True
        return _pids_dead(conn, task_id, run_id)
    except (sqlite3.Error, ValueError, TypeError):
        return False


def comment_live_fields(conn: sqlite3.Connection, task_id: str, live, relayed=None):
    """``(live run id or None, kanban_comment result fields)``: a running card whose worker exited has no live run."""
    if live is not None and worker_exited(conn, task_id, live):
        live, extra = None, {"live_run": False, "worker_exited": True, "delivery": EXITED_NOTE}
    else:
        extra = {"live_run": live is not None}
    if relayed:
        extra["relayed_human"] = relayed
    return live, extra
