"""kanban_complete DB reads: own-park detection and the refusal's PR-acceptance cause. [daveey]

A PR-acceptance refusal (no_required_checks, failing, pending or missing checks) used to reach the worker
tagged ``rejection=stale_run``, so digests counted every PR-CI refusal as a stale run. The structured signal is
the ``pr_acceptance`` event (ok=false) that ``record_acceptance`` appends during the refused call: an event id
above the high-water mark taken just before ``complete_task``.
"""

from __future__ import annotations

import json


GOAL_GATE_MESSAGES = {
    "kanban_complete": {
        "blocked": (
            "Goal completion rejected: judge ruled the goal unachievable — {reason}. The task "
            "will NOT complete silently. Either re-scope the task with kanban_edit, or record "
            "the block with kanban_block and hand the decision to a human / reviewer."),
        "continue": (
            "Goal completion rejected by judge: {reason}. To proceed, either: (1) provide "
            "explicit acceptance evidence in your summary matching the task's criteria, or (2) "
            "create continuation tasks with parents=[{tid}] and keep this task alive.")},
    "kanban_request_review": {
        "blocked": (
            "Goal review handoff rejected: judge ruled the goal unachievable — {reason}. "
            "Record the block with kanban_block instead of requesting review."),
        "continue": (
            "Goal review handoff rejected by judge: {reason}. Provide acceptance evidence "
            "matching the card before requesting review.")}}


def park_unachievable(task, tid, reason, conn):
    """Park a verified judge rejection using the handoff's board and worker identity."""
    from contextlib import nullcontext
    from hermes_cli.kanban_goal_recovery import park
    from tools import kanban_tools as kt
    with (nullcontext((None, conn)) if conn is not None else kt._board(None)) as (_, gate_conn):
        parked = park(gate_conn, task, reason, expected_run_id=kt._worker_run_id(tid))
    if parked:
        raise kt._Reject(f'Goal judged unachievable: {reason}. Parked in triage for bounded '
                         'supervisor recovery; acceptance is unchanged.', 'goal_judge')


def parked_by_own_run(conn, tid: str, run_id: int) -> bool:
    """True when ``run_id`` is the card's latest run, ended by ``kanban_block``, and nothing has
    claimed the card since (no current run, no claim lock, status ready/blocked)."""
    row = conn.execute("SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None or row["current_run_id"] is not None or row["claim_lock"] \
            or row["status"] not in ("ready", "blocked"):
        return False
    last = conn.execute("SELECT id, outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
    return bool(last and last["id"] == run_id and last["outcome"] == "blocked")


def event_mark(conn, tid: str) -> int:
    """The card's highest event id now; events a later call appends have larger ids."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]


def pr_acceptance_refused(conn, tid: str, mark: int) -> bool:
    """True when the completion attempt since ``mark`` recorded a failed, unwaived pr_acceptance event."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'pr_acceptance' AND id > ? "
        "ORDER BY id DESC LIMIT 1", (tid, mark)).fetchone()
    if not row:
        return False
    try:
        payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is False and not payload.get("waived")


def require_completion_metadata(metadata, require_dict) -> None:
    """``require_dict(metadata)``, then the receipt's structure, before the goal judge or any write.

    Owner admission still runs inside canonical ``complete_task``; this only refuses a malformed
    ``completion_receipt`` early so no judge call (and no judge-driven park) precedes the refusal.
    """
    require_dict(metadata)
    if isinstance(metadata, dict) and "completion_receipt" in metadata:
        from hermes_completion_receipt import validate_completion_receipt
        validate_completion_receipt(metadata["completion_receipt"])
