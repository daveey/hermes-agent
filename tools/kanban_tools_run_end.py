"""Run-ended fence and feedback for a worker's own card (agent-6ab88d2b1c). [daveey]

Lives outside ``kanban_tools`` so that file, already over its line cap, does not grow.

A dispatcher worker's run ends at its first successful park/handoff (block, schedule, complete,
continue, request_review), but its process keeps going until the turn ends. Before this module:

- the success payload did not say the run was over, so the worker kept calling tools;
- kanban_link / kanban_unlink did not check the run at all, so the worker could undo its own park
  (unlinking the parent of a dependency park promoted the card back to READY);
- the next lifecycle call was refused by the run-ownership CAS with "unknown id or not in
  running/ready/triage", which hid the real cause.

``refusal`` returns a ``code=run_already_ended`` tool error naming the run, how it ended and the card's
current status; ``success_fields`` adds ``run_ended``/``next`` to a run-ending success. Both apply only to a
dispatcher worker that can name its own run (``HERMES_KANBAN_TASK`` + ``HERMES_KANBAN_RUN_ID``):
orchestrators, operators and the CLI are unaffected.
"""
from __future__ import annotations

import os
from typing import Optional

CODE = "run_already_ended"


def _own_run(task_id: Optional[str] = None) -> tuple[Optional[str], Optional[int]]:
    """``(env task, env run id)`` of this dispatcher worker; ``(None, None)`` outside one.

    With *task_id*, only when the worker is scoped to that task."""
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not env_tid or not raw or (task_id is not None and task_id != env_tid):
        return None, None
    try:
        return env_tid, int(raw)
    except ValueError:
        return None, None


def ended_run(conn, task_id: Optional[str] = None) -> Optional[dict]:
    """``{task_id, run_id, outcome, status}`` when this worker's own run has ended; else None."""
    tid, run_id = _own_run(task_id)
    if run_id is None:
        return None
    run = conn.execute("SELECT ended_at, outcome, status FROM task_runs WHERE id = ? AND task_id = ?",
                       (run_id, tid)).fetchone()
    if run is None or run["ended_at"] is None:
        return None
    task = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    return {"task_id": tid, "run_id": run_id, "outcome": run["outcome"] or run["status"],
            "status": task["status"] if task else "missing"}


def _next(status: str) -> str:
    return (f"This run is over; the card is {status}. Don't call kanban lifecycle tools "
            "(block/schedule/continue/complete/request_review) or kanban_link/kanban_unlink on it "
            "again; write your final reply.")


def refusal(conn, tool_name: str, task_id: Optional[str] = None) -> Optional[str]:
    """A ``run_already_ended`` tool error when this worker's run has ended, else None. Nothing changes."""
    ended = ended_run(conn, task_id)
    if ended is None:
        return None
    from tools.registry import tool_error
    also = ""
    if tool_name == "kanban_schedule":  # keep the human-answer way out (kanban_reason_comment)
        from hermes_cli.kanban_reason_comment import human_answer_refusal
        hint = human_answer_refusal(conn, ended["task_id"], "schedule")
        also = f" Also: {hint}" if hint else ""
    return tool_error(
        f"{tool_name} refused: your run {ended['run_id']} on {ended['task_id']} already ended "
        f"({ended['outcome']}); the card is now {ended['status']}. Nothing changed. "
        f"{_next(ended['status'])}{also}",
        code=CODE, task_id=ended["task_id"], run_id=ended["run_id"],
        run_outcome=ended["outcome"], status=ended["status"], handoff_complete=False)


def bound_worker() -> bool:
    """Whether this process is a dispatcher worker that can name its run (else nothing here applies)."""
    return _own_run()[1] is not None


def guard_refusal(board, args: dict, tool_name: str, tid: str) -> Optional[str]:
    """``refusal`` for ``_worker_guard``; opens the board only for a bound worker."""
    if not bound_worker():
        return None
    with board(args.get("board")) as (_kb, conn):
        return refusal(conn, tool_name, tid)


def annotate_success(board, args: dict, payload: str) -> str:
    """``with_success_fields`` for a committed success, opened on ``board(args['board'])`` for a bound worker
    only. The transition is already committed, so a failed read returns the original success, never an error."""
    import logging
    import sqlite3
    if not bound_worker():
        return payload
    try:
        with board(args.get("board")) as (_kb, conn):
            return with_success_fields(conn, args.get("task_id"), payload)
    except (sqlite3.Error, OSError, ValueError):
        logging.getLogger(__name__).debug("run_ended annotation skipped", exc_info=True)
        return payload


def success_fields(conn, task_id: Optional[str]) -> dict:
    """``run_ended``/``next`` for a success that ended this worker's own run; ``{}`` otherwise."""
    ended = ended_run(conn, task_id)
    if ended is None:
        return {}
    return {"run_ended": True, "next": _next(ended["status"])}


def with_success_fields(conn, task_id: Optional[str], payload: str) -> str:
    """Add ``success_fields`` to a JSON success payload (an existing ``next`` is kept in front)."""
    import json
    extra = success_fields(conn, task_id)
    if not extra:
        return payload
    data = json.loads(payload)  # the caller passes only a '{"ok": true' success payload
    prior = data.get("next")
    data["run_ended"] = True
    data["next"] = f"{prior} {extra['next']}" if prior else extra["next"]
    return json.dumps(data)
