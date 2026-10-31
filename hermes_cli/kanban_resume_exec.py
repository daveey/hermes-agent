"""Supervisor ``resume`` op: requeue a card the continuation budget parked, from a non-worker. [daveey]

A card that hits ``kanban_continue``'s consecutive-continuation limit blocks itself with
``continuation_exhausted`` and lands in ``triage`` (``kanban_continuation`` /
``kanban_block_routing``). Releasing it is agent work, but ``kanban_unblock`` is an
orchestrator tool and the worker shell is fenced from board writes, so a recovery card the
stuck card spawned had no supported route: an assignment repair only swaps the profile, and
every profile's worker has the same wall.

``resume`` is that route, executed by the existing supervisor-request executor
(:mod:`hermes_cli.kanban_supervisor_requests`, non-worker only) through the ordinary
lifecycle API :func:`kanban_db.unblock_task`. It is deliberately narrow:

* **Only budget exhaustion.** The target's newest lifecycle event is a ``blocked`` event
  rekinded ``continuation_exhausted``, written by the same run that recorded the kernel's
  ``continuation_exhausted`` refusal, and nothing has moved the card since. Any other park,
  including a stale one the card already left, is refused.
* **Never a human gate.** A target carrying a ``human_request`` or a ``needs_input`` /
  ``capability`` block is refused (``human_gate``). Prose on a card that a decision "is
  resolved" is not the human's unblock; that transition stays with the human.
* **Scope by provenance.** The target is the requester's own creator chain (the stuck card
  spawned the recovery card, directly or through at most ``MAX_CREATOR_HOPS`` - 1 cards), or a
  card the requester created. ``creator_task_id`` is stamped by the kernel from the creating
  worker's own task, not supplied by the worker.
* **Bounded.** At most ``MAX_RESUMES`` supervisor resumes per target, ever. The 12-run
  continuation limit itself is unchanged; the unblock only ends the trailing ``continued``
  streak the way any deliberate unblock does.
* **Parent gating and graph untouched.** ``unblock_task`` re-gates on parents (``ready`` or
  ``todo``); this op adds and removes no edges.

Execution re-checks everything against the board as it is then. A redelivered request (crash
after apply) finds its own ``unblocked`` event and reports ``already_applied``; anything else
that no longer qualifies fails closed.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

OP = "resume"
MAX_RESUMES = 2
MAX_CREATOR_HOPS = 3
VIA = "supervisor_resume"
HUMAN_BLOCK_KINDS = ("needs_input", "capability")
RESUMABLE_STATUSES = ("triage", "blocked")
# Events that move a card between lifecycle phases. The exhausted ``blocked`` event must be the
# newest of these, so a card that was already released (and maybe parked again for another
# reason) never qualifies on an old exhaustion.
_LIFECYCLE_KINDS = (
    "blocked", "unblocked", "claimed", "promoted", "promoted_manual", "triage_swept", "status",
    "completed", "reopened", "review_requested", "review_reopened", "changes_requested", "archived",
    "scheduled", "continued", "gave_up", "block_loop_detected", "supervisor_escalated",
    "agent_block_escalated", "ask_rechecked", "dependency_wait", "reclaimed", "spawned",
)


def normalize_op(i: int, op: dict) -> dict:
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    tid = str(op.get("task_id") or "").strip()
    if not tid:
        raise SupervisorRequestError("invalid_ops", f"op {i}: resume needs task_id")
    extra = set(op) - {"op", "task_id"}
    if extra:
        raise SupervisorRequestError("invalid_ops", f"op {i}: resume takes only task_id (got {sorted(extra)})")
    return {"op": OP, "task_id": tid}


def label(op: dict) -> str:
    return f"resume {op.get('task_id')}"


def _payload(raw) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _creator(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    from hermes_cli.kanban_supervisor_requests import _created_by_task
    return _created_by_task(conn, task_id)


def scope_refusal(conn: sqlite3.Connection, requester: str, target: str) -> Optional[str]:
    """None when ``target`` is in the requester's creator chain or was created by it."""
    if target == requester:
        return "a card cannot resume itself"
    if _creator(conn, target) == requester:
        return None
    node = requester
    for _ in range(MAX_CREATOR_HOPS):
        node = _creator(conn, node)
        if not node:
            break
        if node == target:
            return None
    return (f"{target} is neither in {requester}'s creator chain (<= {MAX_CREATOR_HOPS} hops) nor a card "
            f"{requester} created; resume only releases the card that spawned this recovery")


def _resume_events(conn: sqlite3.Connection, target: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'unblocked' ORDER BY id",
        (target,)).fetchall()
    return [p for p in (_payload(r[0]) for r in rows) if p.get("via") == VIA]


def applied_by(conn: sqlite3.Connection, target: str, request_id: int) -> bool:
    return any(p.get("request_id") == request_id for p in _resume_events(conn, target))


def exhaustion_refusal(conn: sqlite3.Connection, target: str) -> Optional[str]:
    """None when ``target`` is parked by budget exhaustion and nothing else, else the refusal."""
    row = conn.execute(
        "SELECT status, current_run_id, human_request, block_kind FROM tasks WHERE id = ?",
        (target,)).fetchone()
    if row is None:
        return f"unknown task {target}"
    status, run, human, block_kind = row[0], row[1], row[2], row[3]
    if human or block_kind in HUMAN_BLOCK_KINDS:
        return (f"human_gate: {target} waits on a human ({block_kind or 'human_request'}); only the human's "
                "own unblock releases it, and a comment saying it was decided is not that unblock")
    if status not in RESUMABLE_STATUSES:
        return f"{target} is {status}, not parked in triage/blocked"
    if run is not None:
        return f"{target} still has a live run {run}"
    marks = ",".join("?" for _ in _LIFECYCLE_KINDS)
    last = conn.execute(
        f"SELECT id, kind, run_id, payload FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (target, *_LIFECYCLE_KINDS)).fetchone()
    payload = _payload(last[3]) if last else {}
    if not last or last[1] != "blocked" or payload.get("rekind_reason") != "continuation_exhausted":
        return (f"{target}'s current park is not a continuation_exhausted block "
                f"(newest lifecycle event: {last[1] if last else 'none'})")
    if last[2] is None or not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'continuation_exhausted' "
            "AND id < ?", (target, last[2], last[0])).fetchone():
        return f"{target}'s block was not written by the run the kernel refused a continuation"
    return None


def preflight_op(conn: sqlite3.Connection, requester: str, op: dict) -> dict:
    target = op["task_id"]
    verdict = {**op}
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (target,)).fetchone()
    refusal = (None if row else f"unknown task {target}") or (
        f"{target} is archived" if row[0] == "archived" else None)
    refusal = refusal or scope_refusal(conn, requester, target) or exhaustion_refusal(conn, target)
    if refusal is None and len(_resume_events(conn, target)) >= MAX_RESUMES:
        refusal = (f"resume_budget_exhausted: {target} was already supervisor-resumed {MAX_RESUMES} times; "
                   "split the work instead of resuming it again")
    if refusal:
        verdict.update(verdict="refused", reason=refusal)
    else:
        verdict.update(verdict="supervisor")
    return verdict


def _readback(conn: sqlite3.Connection, target: str) -> dict:
    from hermes_cli import kanban_db as kb
    t = kb.get_task(conn, target)
    return {"status": t.status if t else None, "current_run_id": t.current_run_id if t else None,
            "unsatisfied_parents": [p for p, _ in kb.unsatisfied_parents(conn, target)]}


def execute(conn: sqlite3.Connection, req: dict, executor: str) -> str:
    """Apply a resume request's ops (re-checked now); record the receipt via ``_finish``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_supervisor_requests as ksr

    requester, rid = req["requester_task_id"], int(req["id"])
    gone = ksr.requester_refusal(conn, requester)
    results: list[dict] = []
    error: Optional[str] = None
    for op in req["ops"]:
        target = op["task_id"]
        if applied_by(conn, target, rid):
            results.append({**op, "outcome": "already_applied", **_readback(conn, target)})
            continue
        verdict = {"verdict": "refused", "reason": gone} if gone else preflight_op(conn, requester, op)
        if error or verdict["verdict"] != "supervisor":
            detail = "an earlier op failed" if error and verdict["verdict"] == "supervisor" else verdict.get("reason")
            results.append({**op, "outcome": "not_applied", "detail": detail})
            error = error or detail
            continue
        note = (f"Resumed by supervisor request #{rid} from {requester} (executor {executor}): the "
                "continuation budget had parked this card. Parent gating applies; acceptance unchanged.")
        ok = kb.unblock_task(conn, target, comment=(ksr.COMMENT_AUTHOR, note),
                             event_extra={"via": VIA, "request_id": rid, "by": requester})
        if ok and applied_by(conn, target, rid):
            results.append({**op, "outcome": "applied", **_readback(conn, target)})
        else:
            error = f"unblock_task did not move {target}"
            results.append({**op, "outcome": "failed", "detail": error, **_readback(conn, target)})
    applied = any(r["outcome"] in ("applied", "already_applied") for r in results)
    status = "done" if not error else ("failed" if applied or any(r["outcome"] == "failed" for r in results)
                                       else "rejected")
    ksr._finish(conn, req, executor, status, results=results, error=error)
    return status
