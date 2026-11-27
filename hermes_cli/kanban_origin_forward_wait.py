"""Refuse a park that waits on a product key this run's own creation definitively lost.

``kanban_create`` returns a queued ``wait-check key`` handle before the origin gateway has
judged the forward.  A later ``origin_forward_binding_missing`` rejection happens before any
owner create, so no card with that key exists or ever will from that request, and the probe
would sleep to its backstop.  ``origin_forward_run_end`` deliberately lets that definitive
refusal end the run; this module only stops the creator from *waiting* on the lost child.

Read-only evidence, never authority: nothing here inherits, mints or rebinds an origin
binding, edits a frozen request, or treats a replica as an accepted receipt.  Missing,
unreadable or ambiguous courier evidence is "cannot tell", never a refusal.
"""
from __future__ import annotations

from contextlib import closing
import json
import shlex
import sqlite3
from typing import Optional

CODE = "origin_forward_rejected_wait"
_REFUSED = "origin_forward_binding_missing"


def wait_key(wake_check: Optional[str]) -> Optional[str]:
    """The product key a ``hermes kanban wait-check key <KEY> ...`` probe waits on, else None."""
    try:
        words = shlex.split(wake_check or "")
    except ValueError:
        return None
    for i in range(len(words) - 4):
        if words[i:i + 3] == ["hermes", "kanban", "wait-check"] and words[i + 3] == "key":
            key = words[i + 4]
            return key if key and not key.startswith("-") else None
    return None


def _run_uid(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    from hermes_cli import kanban_db_sync as sync
    if "uid" not in sync.table_columns(conn, "task_runs"):
        return None  # unsynced boards carry no portable run identity, as in require_settled
    row = conn.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id=t.current_run_id "
                       "WHERE t.id=? AND r.task_id=t.id AND r.ended_at IS NULL", (task_id,)).fetchone()
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def _attempts(task_id: str, key: str) -> Optional[list[tuple[str, str, dict, str]]]:
    """``(request id, state, result, run uid)`` per courier row creating ``key`` for this creator."""
    from hermes_cli.kanban_capability_registry import hub_db_path, _ro
    try:
        path = hub_db_path()
        path.lstat()
        with closing(_ro(path)) as db:
            rows = db.execute("SELECT id,state,result,body FROM sent WHERE body LIKE '%origin-forward%'").fetchall()
    except (sqlite3.Error, OSError):
        return None
    found = []
    for rid, state, raw_result, raw_body in rows:
        try:
            body = json.loads(raw_body)
            args = body["args"]
            if (body.get("coll") != "sessions" or body.get("action") != "origin-forward"
                    or args["creator"]["task_id"] != task_id
                    or args["create"]["idempotency_key"] != key):
                continue
            run_uid = args.get("run_uid")
        except (ValueError, TypeError, KeyError, AttributeError):
            continue  # a row we cannot attribute is neither this creator's nor evidence
        try:
            result = json.loads(raw_result or "{}")
        except (ValueError, TypeError):
            result = None
        found.append((str(rid), state, result if isinstance(result, dict) else {}, run_uid))
    return found


def _definitive(state, result: dict) -> bool:
    """The exact pre-create refusal ``kanban_placement._forward_binding_retry`` also recognizes."""
    return (state == "rejected" and result.get("code") == _REFUSED
            and result.get("status") == "rejected" and result.get("handoff_complete") is False)


def rejected_wait(conn: sqlite3.Connection, task_id: str, wake_check: Optional[str]) -> Optional[str]:
    """Refusal text when this park waits on a key whose creation this run definitively failed."""
    key = wait_key(wake_check)
    if key is None:
        return None
    from hermes_cli.kanban_workspace_publication import _kernel_park
    if _kernel_park.get() == task_id:
        return None  # mandatory kernel parks stay possible, as for the pending gate
    run_uid = _run_uid(conn, task_id)
    attempts = _attempts(task_id, key) if run_uid else None
    if not attempts or not all(_definitive(state, result) for _, state, result, _ in attempts):
        return None  # no evidence, a recovery/in-flight/done request, or another failure: not this gate
    mine = [rid for rid, _, _, uid in attempts if uid == run_uid]
    if not mine:
        return None  # an earlier run's refusal; the owner may have recovered the key since
    return (f"{CODE}: this run's creation of the child with idempotency key {key} (request "
            f"{sorted(mine)[0]}) was definitively refused as {_REFUSED}, before any card was created, so "
            "`wait-check key` can never see it. Do not park on it and do not retry creation in this run. "
            "End the run reporting the lost child (kanban_complete or kanban_block with the refusal; the "
            "owning chat already holds the rejection notice), or, if the owning chat created the card "
            "another way, wait on that card's id instead. No origin binding was inherited, minted or changed.")
