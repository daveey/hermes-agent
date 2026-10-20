"""Timed parks: a block that waits on a clock or an outside event, not a person.

Workers park cards that wait on time (a soak, a nightly run, a release window, a
cron that re-checks) with ``kanban_block``, because goal-mode cards may not call
``kanban_schedule``. ``block_task`` used to re-kind a parent-less ``dependency``
wait to ``needs_input``, so the card sat in ``blocked`` and the Desktop sidebar
painted its chat red as "needs a human" although nobody had anything to do.

``block_task`` now routes such a wait here instead: the card lands in
``scheduled`` (not dispatchable, counted as in-progress, never red) with a wake
time on its ``scheduled`` event, which ``wake_due_continuations`` honours like a
``kanban_continue`` wait. A ``hermes kanban unblock`` (a wake cron) still wakes it
earlier. A timed park is one of:

* ``kind='time'``;
* ``kind='dependency'`` with no open parent (the wait is on something off the board);
* any kind whose reason carries ``SCHEDULED_UNTIL=<ISO8601>``.

Without an explicit time the card wakes after ``DEFAULT_WAKE_SECONDS`` so a park
can never sit forgotten: the next run re-checks and parks again. [daveey]
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

TIME_KIND = "time"
DEFAULT_WAKE_SECONDS = 24 * 3600
# An explicit SCHEDULED_UNTIL may park longer than a continuation wait, but not forever.
MAX_WAKE_SECONDS = 14 * 24 * 3600
MIN_WAKE_SECONDS = 60
_MARKER = re.compile(r"SCHEDULED_UNTIL=(\S+)")


def scheduled_until(reason: Optional[str], *, now: Optional[int] = None) -> Optional[int]:
    """Epoch seconds named by a ``SCHEDULED_UNTIL=<ISO8601>`` marker, clamped to
    ``[now + MIN_WAKE_SECONDS, now + MAX_WAKE_SECONDS]``; None when absent or unparseable.
    A naive timestamp is local time."""
    match = _MARKER.search(reason or "")
    if not match:
        return None
    raw = match.group(1).rstrip(".,;:)]}'\"")
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    now = int(time.time()) if now is None else int(now)
    epoch = int(when.timestamp())
    return max(now + MIN_WAKE_SECONDS, min(epoch, now + MAX_WAKE_SECONDS))


def scheduled_payload(reason: Optional[str]) -> dict:
    """``scheduled`` event payload for ``kanban_schedule``: a ``SCHEDULED_UNTIL=`` marker is a
    real timer (``wake_due_continuations``); without one the card waits for a cron/human."""
    wake_at = scheduled_until(reason)
    return {"reason": reason, "wake_at": wake_at} if wake_at else {"reason": reason}


def is_timed_park(kind: Optional[str], reason: Optional[str], *, parents_satisfied: bool) -> bool:
    """Whether a ``block_task`` call is really a wait on time, not on a human."""
    if kind == TIME_KIND or (kind == "dependency" and parents_satisfied):
        return True
    return _MARKER.search(reason or "") is not None


def park(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str], requested_kind: Optional[str],
    source_status: str = "ready", expected_run_id: Optional[int] = None,
) -> bool:
    """``running``/``ready`` -> ``scheduled`` with a wake time. Caller holds the write txn."""
    from hermes_cli import kanban_db as kb

    now = int(time.time())
    wake_at = scheduled_until(reason, now=now) or now + DEFAULT_WAKE_SECONDS
    sql = ("UPDATE tasks SET status = 'scheduled', claim_lock = NULL, claim_expires = NULL, "
           "worker_pid = NULL WHERE id = ? AND status IN ('running', 'ready')")
    params: tuple = (task_id,)
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params = (task_id, int(expected_run_id))
    if conn.execute(sql, params).rowcount != 1:
        return False
    run_id = kb._end_or_synthesize_run(
        conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
    )
    kb._append_event(conn, task_id, "scheduled", {
        "reason": reason, "timed_park": True, "requested_kind": requested_kind,
        "wake_at": wake_at, "resume_status": source_status,
    }, run_id=run_id)
    return True
