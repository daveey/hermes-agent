"""Tell an unreachable profile owner from a worker crash in the dead-worker sweep. [daveey]

A dispatcher worker (``kanban_worker_client``) only submits its claim to the profile owner; the owner
runs the turn. When the owner stays unreachable (a restart's ``restart_wait`` drain, a starting or
draining gateway) for ``OWNER_WAIT_S`` before it answers ``kanban.run``, the worker exits
``KANBAN_OWNER_UNAVAILABLE_EXIT_CODE``. No turn ran, so the card did nothing wrong: on 2026-10-30
t_82f0fe15 runs 7630/7649 each waited out a ``restart_wait`` drain, exited 1, were booked ``crashed``
and spent the card's whole failure budget (triage), while the next run after the restart parked cleanly.

Such a run is booked ``owner_unavailable`` and requeued WITHOUT counting a failure, unless:

- the board holds an ``owner_admitted`` marker for the run (the owner did admit it, so a turn may
  have run and its loss is a real failure), or
- the card's last ``NEUTRAL_STREAK_LIMIT`` closed runs were all ``owner_unavailable`` (an owner that
  never comes back must still reach the breaker instead of respawning forever).

Either way the run falls back to the ordinary ``crashed`` booking.
"""
from __future__ import annotations

import sqlite3

OWNER_UNAVAILABLE_OUTCOME = "owner_unavailable"
EXIT_KIND = "owner_unavailable"
# The worker's exit code for it (``kanban_worker_client``). 69 == BSD EX_UNAVAILABLE.
KANBAN_OWNER_UNAVAILABLE_EXIT_CODE = 69
NEUTRAL_STREAK_LIMIT = 5


def _owner_admitted(conn: sqlite3.Connection, task_id: str, run_id) -> bool:
    if run_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'owner_admitted' LIMIT 1",
        (task_id, int(run_id)),
    ).fetchone()
    return row is not None


def _neutral_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Trailing closed runs booked ``owner_unavailable``, newest first."""
    streak = 0
    for row in conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT ?",
        (task_id, NEUTRAL_STREAK_LIMIT + 1),
    ):
        if row[0] != OWNER_UNAVAILABLE_OUTCOME:
            break
        streak += 1
    return streak


def noted(dead, conn, task_id, run_id):
    """``note`` then return *dead* (one expression at the call site)."""
    note(dead, conn, task_id, run_id)
    return dead


def note(dead, conn, task_id, run_id) -> None:
    """Rebook a dead worker (``_DeadWorker``) that exited owner-unavailable as a neutral requeue, or
    turn it back into a plain nonzero-exit crash when the run cannot be proven turn-free. No-op for
    any other exit."""
    if dead.kind != EXIT_KIND:
        return
    reason = None
    if conn is None or not task_id:
        reason = "no board connection to check the admission"
    elif _owner_admitted(conn, task_id, run_id):
        reason = "the owner had admitted this run"
    elif _neutral_streak(conn, task_id) >= NEUTRAL_STREAK_LIMIT:
        reason = f"{NEUTRAL_STREAK_LIMIT} runs in a row found no owner"
    if reason is not None:
        dead.kind = "nonzero_exit"
        dead.event_payload["exit_kind"] = "nonzero_exit"
        dead.error_text += f" (profile owner unreachable; counted as a failure: {reason})"
        return
    dead.owner_unavailable = True
    dead.event_kind = OWNER_UNAVAILABLE_OUTCOME
    dead.event_payload["owner_unavailable"] = True
    dead.error_text = (
        f"pid {dead.event_payload.get('pid')}: the profile owner never answered the claim "
        "(restarting or draining); no turn ran, requeued without counting a failure"
    )
