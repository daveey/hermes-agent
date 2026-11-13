"""Tell an unreachable profile owner from a worker crash in the dead-worker sweep. [daveey]

A dispatcher worker (``kanban_worker_client``) only submits its claim to the profile owner; the owner
runs the turn. When the owner stays unreachable (a restart's ``restart_wait`` drain, a starting or
draining gateway) for ``OWNER_WAIT_S`` before it answers ``kanban.run``, the worker exits
``KANBAN_OWNER_UNAVAILABLE_EXIT_CODE``. No turn ran, so the card did nothing wrong: on 2026-10-30
t_82f0fe15 runs 7630/7649 each waited out a ``restart_wait`` drain, exited 1, were booked ``crashed``
and spent the card's whole failure budget (triage), while the next run after the restart parked cleanly.

Such a run is booked ``owner_unavailable`` and requeued WITHOUT counting a failure, unless:

- the board holds an ``owner_admitted`` marker for the run (the owner did admit it, so a turn may
  have run and its loss is a real failure), unless the run's bound interpreter proved it died
  constructing its agent on the host's routing registry (empty, or unreadable/invalid:
  ``kanban_host_refusal.proven``: no turn ran, and no card's pin could have passed), or its final
  bound-worker receipt proves an owner fault mid-turn: a storage refusal or an owner it could not
  reach (``kanban_owner_storage_refusal``), or
- the card's last ``NEUTRAL_STREAK_LIMIT`` closed runs were all ``owner_unavailable`` (an owner that
  never comes back must still reach the breaker instead of respawning forever).

Either way the run falls back to the ordinary ``crashed`` booking.
"""
from __future__ import annotations

import re
import sqlite3

from hermes_cli.kanban_host_refusal import PROOF_KEY, proven, run_error

OWNER_UNAVAILABLE_OUTCOME = "owner_unavailable"
EXIT_KIND = "owner_unavailable"
# A worker that found its owner's service failed/masked/disabled (``kanban_owner_down``): booked the same
# way (requeued uncounted, the same admission and streak limits), under its own outcome. [daveey]
OWNER_DOWN_KIND = "owner_down"
_NEUTRAL_OUTCOMES = (OWNER_UNAVAILABLE_OUTCOME, OWNER_DOWN_KIND)
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
        if row[0] not in _NEUTRAL_OUTCOMES:
            break
        streak += 1
    return streak


# A worker client older than exits 69/76 (still imported in a worker that was in flight when the
# gateway restart installing them began) gives up with exit 1 and this as its final line. Only the
# client's own words: it prints nothing of the agent's before refusing. [daveey]
_LEGACY_REFUSAL_RE = re.compile(
    r"^Kanban owner refused: Kanban owner silent for \d+s: Gateway (?:draining|starting)\b")


def legacy_refusal_in_log(log: str) -> bool:
    """True when the dead run's own part of the log ends with the old client's drain refusal."""
    from hermes_cli.kanban_worker_log import current_run_log
    from hermes_cli.kanban_db_dispatch import _EXIT_TRAILER_RE
    lines = [ln.strip() for ln in _EXIT_TRAILER_RE.sub("", current_run_log(log or "")).splitlines() if ln.strip()]
    return bool(lines) and bool(_LEGACY_REFUSAL_RE.match(lines[-1]))


def _note_legacy(dead, conn, task_id, run_id, log) -> None:
    """Transition drain: rebook an old client's exit-1 drain refusal as the exit the new client would
    have used. Admitted (a turn ran, the restart took it) -> owner restart (76); else -> 69. [daveey]"""
    if not (dead.kind == "nonzero_exit" and dead.code == 1 and log and legacy_refusal_in_log(log)):
        return
    if conn is not None and task_id and _owner_admitted(conn, task_id, run_id):
        pid = dead.event_payload.get("pid")
        dead.kind, dead.owner_restart, dead.event_kind = "owner_restart", True, "owner_restart_lost"
        dead.event_payload.update(exit_kind="owner_restart", owner_restart=True, detected_by="log")
        dead.error_text = (f"pid {pid} lost its turn to a gateway restart (pre-fix worker's drain "
                           "refusal) — requeued without counting a failure")
        return
    dead.kind = EXIT_KIND
    dead.event_payload["detected_by"] = "log"


def noted(dead, conn, task_id, run_id, log=None):
    """``note`` then return *dead* (one expression at the call site). A requeued host refusal (a
    startup registry refusal, or a pre-/post-admission ``storage_unavailable``) is surfaced here, in the
    sweep's transaction: once per window per host, never once per card."""
    _note_legacy(dead, conn, task_id, run_id, log)
    note(dead, conn, task_id, run_id)
    if dead.owner_unavailable and dead.event_payload.get(PROOF_KEY):
        from hermes_cli.kanban_host_refusal import notify
        notify(conn, task_id, dead.event_payload[PROOF_KEY])
    return dead


def note(dead, conn, task_id, run_id) -> None:
    """Rebook owner unavailability or a proven mid-turn owner-store refusal as a neutral requeue.

    Admission requires a startup or bound-worker storage proof; other exits remain crashes.
    """
    storage_proof = None
    if dead.kind == "nonzero_exit" and dead.code == 1:
        from hermes_cli.kanban_owner_storage_refusal import proven as storage_refusal
        storage_proof = storage_refusal(conn, task_id, run_id, dead.event_payload.get("claimer"))
        if storage_proof:
            dead.kind = EXIT_KIND
            dead.event_payload["exit_kind"] = EXIT_KIND
    if dead.kind not in (EXIT_KIND, OWNER_DOWN_KIND):
        return
    owner_down = dead.kind == OWNER_DOWN_KIND
    reason = proof = None
    if conn is None or not task_id:
        reason = "no board connection to check the admission"
    else:
        admitted = _owner_admitted(conn, task_id, run_id)
        # Admitted: only a bound interpreter's startup/storage proof excuses it. Not admitted: requeued as
        # before; a submitter's pre-admission proof only names the host fault for the notice.
        proof = storage_proof or proven(conn, task_id, run_id, dead.event_payload.get("claimer"), admitted=admitted,
                       pid=dead.event_payload.get("pid"))
        if admitted and not proof:
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
    if owner_down:
        dead.owner_down = True
        dead.event_kind = OWNER_DOWN_KIND
        dead.event_payload["owner_down"] = True
        dead.error_text = (f"pid {dead.event_payload.get('pid')}: the profile owner's gateway service is "
                           "failed, masked or disabled; no turn ran, requeued without counting a failure "
                           "(the dispatcher holds the card until the owner is back)")
        return
    if proof:
        dead.event_payload[PROOF_KEY] = proof
        dead.error_text = run_error(dead.event_payload.get("pid"), proof)
        return
    dead.error_text = (
        f"pid {dead.event_payload.get('pid')}: the profile owner never answered the claim "
        "(restarting or draining); no turn ran, requeued without counting a failure"
    )
