"""Supervisor ``archive`` op: cancel an unstarted card orphaned in the requester's creator tree. [daveey]

A worker may archive an unclaimed card its own task created (``kanban_archive``), but not a
grandchild: when the requester created card B, B created card C, and B is then archived or done,
nobody owns C any more. ``kanban_archive`` from the requester is refused (C's creator is B), B
cannot act (it is finished), and ``kanban_block(kind='assignment')`` answers
``no_capable_supervisor`` because the creator and assignee profile are the same. The only way out
was an operator archiving C by hand. (Ask Eve QA card t_01a1947dae56913a: its creator
t_01a19446d2578c6a was archived when the PR it reviewed was closed, and the standing card that
created that creator could not cancel it.)

``archive`` is that cancellation route, executed by the existing non-worker supervisor-request
executor (:mod:`hermes_cli.kanban_supervisor_requests`) through :func:`kanban_db.archive_task`.
It is deliberately narrow:

* **Only an orphaned descendant.** Walking ``creator_task_id`` up from the target must reach the
  requester within ``MAX_CREATOR_HOPS``, and every card between them must be archived or done (a
  live intermediate still owns the target and archives it itself). A card the requester created
  directly is refused with a pointer to ``kanban_archive``: the worker can do that itself.
* **Never the requester's own card** (``self_scope``), never a card outside its creator tree.
* **Unstarted cards only.** The target must be unclaimed (no claim, no worker pid, no current run)
  and not done or archived: this cancels work, it never stops a running worker.

Execution re-checks scope, target state and the requester against the board as it is now. The
``archived`` event carries the request id; a redelivered request whose target is already archived
reports ``already_applied`` instead of writing twice.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

OP = "archive"
MAX_CREATOR_HOPS = 3
VIA = "supervisor archive request"
_FINISHED = ("archived", "done")
_UNSTARTED = ("todo", "ready", "scheduled", "blocked", "triage")


def normalize_op(i: int, op: dict) -> dict:
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    tid = str(op.get("task_id") or "").strip()
    if not tid:
        raise SupervisorRequestError("invalid_ops", f"op {i}: archive needs task_id")
    extra = set(op) - {"op", "task_id"}
    if extra:
        raise SupervisorRequestError("invalid_ops", f"op {i}: archive takes only task_id (got {sorted(extra)})")
    return {"op": OP, "task_id": tid}


def label(op: dict) -> str:
    return f"archive {op.get('task_id')}"


def _status(conn: sqlite3.Connection, target: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (target,)).fetchone()
    return row[0] if row else None


def _scope_refusal(conn: sqlite3.Connection, requester: str, target: str) -> Optional[str]:
    """None when ``target`` is an orphaned descendant of ``requester`` in the creator tree."""
    from hermes_cli.kanban_supervisor_requests import _created_by_task
    if target == requester:
        return "self_scope: a card cannot archive itself; finish it with kanban_complete"
    creator = _created_by_task(conn, target)
    if creator == requester:
        return (f"{target} was created by {requester}: archive it yourself with kanban_archive "
                "(no supervisor needed)")
    chain: list[str] = []
    node = creator
    for _ in range(MAX_CREATOR_HOPS):
        if not node:
            break
        if node == requester:
            live = [c for c in chain if _status(conn, c) not in _FINISHED]
            if live:
                return (f"{target}'s creator {live[0]} is still open, status={_status(conn, live[0])}; "
                        "that card owns it and archives it itself")
            return None
        chain.append(node)
        node = _created_by_task(conn, node)
    return (f"{target} is not in {requester}'s creator tree (<= {MAX_CREATOR_HOPS} hops); archive only "
            "cancels an orphaned card created by a finished card this task created")


def _state_refusal(conn: sqlite3.Connection, target: str) -> Optional[str]:
    row = conn.execute("SELECT status, claim_lock, worker_pid, current_run_id FROM tasks WHERE id = ?",
                       (target,)).fetchone()
    if row is None:
        return f"unknown task {target}"
    status, claim, pid, run = row
    if status in _FINISHED:
        return f"{target} is {status}"
    if status not in _UNSTARTED or claim or pid or run:
        return (f"{target} is claimed or running (status={status}); archive only cancels an unstarted "
                "card and never stops a worker")
    return None


def refusal(conn: sqlite3.Connection, requester: str, target: str) -> Optional[str]:
    if _status(conn, target) is None:
        return f"unknown task {target}"
    return _scope_refusal(conn, requester, target) or _state_refusal(conn, target)


def preflight_op(conn: sqlite3.Connection, requester: str, op: dict) -> dict:
    verdict = {**op}
    target = op["task_id"]
    if _status(conn, target) == "archived" and _scope_refusal(conn, requester, target) is None:
        verdict.update(verdict="noop", reason=f"{target} is already archived")
        return verdict
    found = refusal(conn, requester, target)
    if found:
        verdict.update(verdict="refused", reason=found)
    else:
        verdict.update(verdict="supervisor")
    return verdict


def applied_by(conn: sqlite3.Connection, target: str, request_id: int) -> bool:
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived'",
                               (target,)):
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("supervisor_request_id") == request_id:
            return True
    return False


def execute(conn: sqlite3.Connection, req: dict, executor: str) -> str:
    """Archive each op's target (re-checked now); record the receipt via ``_finish``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_supervisor_requests as ksr

    requester, rid = req["requester_task_id"], int(req["id"])
    gone = ksr.requester_refusal(conn, requester)
    results: list[dict] = []
    error: Optional[str] = None
    for op in req["ops"]:
        target = op["task_id"]
        if applied_by(conn, target, rid):
            results.append({**op, "outcome": "already_applied", "readback": _status(conn, target)})
            continue
        found = gone or refusal(conn, requester, target)
        if error or found:
            detail = found or "an earlier op failed"
            results.append({**op, "outcome": "not_applied", "detail": detail, "readback": _status(conn, target)})
            error = error or detail
            continue
        ok = kb.archive_task(conn, target, via=f"{VIA} {rid} by {requester}",
                             event_extra={"supervisor_request_id": rid, "requested_by": requester,
                                          "executor": executor})
        readback = _status(conn, target)
        if not ok or readback != "archived":
            error = f"archive_task did not archive {target} (readback {readback})"
            results.append({**op, "outcome": "failed", "detail": error, "readback": readback})
            continue
        results.append({**op, "outcome": "applied", "readback": readback})
    applied = any(r["outcome"] in ("applied", "already_applied") for r in results)
    status = "done" if not error else (
        "failed" if applied or any(r["outcome"] == "failed" for r in results) else "rejected")
    ksr._finish(conn, req, executor, status, results=results, error=error)
    return status
