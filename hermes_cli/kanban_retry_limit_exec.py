"""Supervisor ``retry_limit`` op: set the retry breaker on a card the requester created. [daveey]

A worker that fans out a flaky card (a runner job, a CI probe) may need a wider retry breaker
than the config default, but ``kanban_create`` only sets it at creation and the worker shell is
fenced from ``hermes kanban edit`` (``_is_delegated_child_cli_mutation``). ``retry_limit`` is
the edit route, executed by the existing non-worker supervisor-request executor
(:mod:`hermes_cli.kanban_supervisor_requests`) through the operator API
:func:`hermes_cli.kanban_db_operator.edit_operator_fields`, which writes only ``max_retries`` and
appends the ordinary ``edited`` event. It is deliberately narrow:

* **Only cards the requester created.** ``creator_task_id`` is stamped by the kernel from the
  creating worker's own task. Not the creator chain, not siblings, not grandchildren.
* **Never the requester's own card** (``self_scope``): a worker must not widen its own breaker.
* **Bounded.** ``max_retries`` is an int 1..``MAX_RETRY_LIMIT``; bools are refused.
* **Live cards only.** Archived or done targets, and a finished requester, are refused.

Execution re-checks scope and the requester against the board as it is now. Each applied op
appends a ``supervisor_retry_limit`` event carrying the request id, so a redelivered request
(crash after the write, before the receipt) reports ``already_applied`` instead of writing twice.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

OP = "retry_limit"
MAX_RETRY_LIMIT = 10
MARK = "supervisor_retry_limit"
_DEAD_STATUSES = ("archived", "done")


def normalize_op(i: int, op: dict) -> dict:
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    tid = str(op.get("task_id") or "").strip()
    if not tid:
        raise SupervisorRequestError("invalid_ops", f"op {i}: retry_limit needs task_id")
    extra = set(op) - {"op", "task_id", "max_retries"}
    if extra:
        raise SupervisorRequestError(
            "invalid_ops", f"op {i}: retry_limit takes only task_id and max_retries (got {sorted(extra)})")
    n: Any = op.get("max_retries")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_RETRY_LIMIT:
        raise SupervisorRequestError(
            "invalid_ops", f"op {i}: retry_limit max_retries must be an integer 1-{MAX_RETRY_LIMIT} (got {n!r})")
    return {"op": OP, "task_id": tid, "max_retries": int(n)}


def label(op: dict) -> str:
    return f"retry_limit {op.get('task_id')} -> {op.get('max_retries')}"


def _current(conn: sqlite3.Connection, target: str) -> Optional[int]:
    row = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (target,)).fetchone()
    return row[0] if row else None


def refusal(conn: sqlite3.Connection, requester: str, target: str) -> Optional[str]:
    """None when ``target`` is a live card the requester created (and not the requester itself)."""
    from hermes_cli.kanban_supervisor_requests import _created_by_task
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (target,)).fetchone()
    if row is None:
        return f"unknown task {target}"
    if target == requester:
        return "self_scope: a card cannot change its own retry breaker"
    if row[0] in _DEAD_STATUSES:
        return f"{target} is {row[0]}"
    if _created_by_task(conn, target) != requester:
        return (f"{target} was not created by {requester}; retry_limit only edits cards this task "
                "created (not its creator chain, siblings or their children)")
    return None


def preflight_op(conn: sqlite3.Connection, requester: str, op: dict) -> dict:
    verdict = {**op}
    found = refusal(conn, requester, op["task_id"])
    if found:
        verdict.update(verdict="refused", reason=found)
    elif _current(conn, op["task_id"]) == op["max_retries"]:
        verdict.update(verdict="noop", reason=f"max_retries is already {op['max_retries']}")
    else:
        verdict.update(verdict="supervisor")
    return verdict


def _marks(conn: sqlite3.Connection, target: str) -> list[dict]:
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                        (target, MARK)).fetchall()
    out = []
    for (raw,) in rows:
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def applied_by(conn: sqlite3.Connection, target: str, request_id: int) -> bool:
    return any(m.get("request_id") == request_id for m in _marks(conn, target))


def execute(conn: sqlite3.Connection, req: dict, executor: str) -> str:
    """Apply a retry_limit request's ops (re-checked now); record the receipt via ``_finish``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_operator as kdo
    from hermes_cli import kanban_supervisor_requests as ksr

    requester, rid = req["requester_task_id"], int(req["id"])
    gone = ksr.requester_refusal(conn, requester)
    results: list[dict] = []
    error: Optional[str] = None
    for op in req["ops"]:
        target, want = op["task_id"], op["max_retries"]
        if applied_by(conn, target, rid):
            results.append({**op, "outcome": "already_applied", "readback": _current(conn, target)})
            continue
        found = gone or refusal(conn, requester, target)
        if error or found:
            detail = found or "an earlier op failed"
            results.append({**op, "outcome": "not_applied", "detail": detail})
            error = error or detail
            continue
        old = _current(conn, target)
        if old == want:
            # Already so: this request's write landed before a crash took the mark with it, or another
            # route set the same value. Either way the requested state holds; never write twice.
            results.append({**op, "outcome": "already_applied", "readback": old})
            continue
        try:
            changes = kdo.edit_operator_fields(conn, target, max_retries=want)
        except kdo.KanbanEditRefused as exc:
            changes, error = None, str(exc)
        readback = _current(conn, target)
        if changes is None or readback != want:
            error = error or f"edit_operator_fields did not set {target}.max_retries to {want}"
            results.append({**op, "outcome": "failed", "detail": error, "readback": readback})
            continue
        with kb.write_txn(conn):
            kb._append_event(conn, target, MARK, {"request_id": rid, "by": requester, "executor": executor,
                                                  "old": old, "new": want})
        results.append({**op, "outcome": "applied", "old": old, "new": want, "readback": readback})
    applied = any(r["outcome"] in ("applied", "already_applied") for r in results)
    status = "done" if not error else (
        "failed" if applied or any(r["outcome"] == "failed" for r in results) else "rejected")
    ksr._finish(conn, req, executor, status, results=results, error=error)
    return status
