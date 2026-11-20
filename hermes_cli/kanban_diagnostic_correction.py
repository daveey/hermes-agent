"""Quote diagnostic prose without rewriting the text it describes. [daveey]

Quotation planning is pure. It grants no authority to mutate a card or comment.
"""
from __future__ import annotations

import os
import re
import sqlite3


def command(args) -> int:
    """Read a bounded exact request; no actor/force/authority flags exist."""
    import json
    import sys

    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_review_required import worker_identity

    if worker_identity():
        print("kanban: diagnostic correction requires an independent non-worker operator", file=sys.stderr)
        return 2
    keys = {"comment_uid", "expected_body", "expected_comment", "body_diagnostic",
            "comment_diagnostic", "reason", "request_id"}
    try:
        with open(args.request_file, "rb") as source:
            raw = source.read(200001)
        if len(raw) > 200000:
            raise ValueError("file exceeds 200000 bytes")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or set(request) != keys:
            raise ValueError("expected exactly: " + ", ".join(sorted(keys)))
    except (OSError, ValueError, RecursionError) as exc:
        print(f"kanban: invalid correction request: {exc}", file=sys.stderr)
        return 2
    try:
        with connect_closing() as conn:
            result = correct_diagnostics(conn, args.task_id, **request)
    except (ValueError, RuntimeError, PermissionError, sqlite3.Error) as exc:
        print(f"kanban: correction refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def correct_diagnostics(
    conn: sqlite3.Connection, task_id: str, *, comment_uid: str,
    expected_body: str, expected_comment: str, body_diagnostic: str,
    comment_diagnostic: str, reason: str, request_id: str,
) -> dict:
    """Operator-only local CAS; sync convergence must be verified separately.

    Caller-provided prose is an audit reason, never an authorization grant.
    This uses the existing trusted-operator model: absence of worker markers
    does not authenticate a human or author chat. The actual authorized operator
    must initiate the call; process_context is observation, not identity proof.
    Retain the existing process/DB fences and verify invocation custody outside
    this function before claiming independent-author delivery.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_review_required import worker_identity

    if worker_identity():
        raise PermissionError("diagnostic correction requires an independent non-worker operator")
    fields = {
        "task_id": (task_id, 64), "comment_uid": (comment_uid, 201),
        "expected_body": (expected_body, 100000), "expected_comment": (expected_comment, 50000),
        "body_diagnostic": (body_diagnostic, 2000), "comment_diagnostic": (comment_diagnostic, 2000),
        "reason": (reason, 2000), "request_id": (request_id, 128),
    }
    for name, (value, limit) in fields.items():
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
            raise ValueError(f"invalid correction field: {name}")
    after = {
        "body": quote_diagnostic(expected_body, body_diagnostic),
        "comment": quote_diagnostic(expected_comment, comment_diagnostic),
    }
    with kb.write_txn(conn):
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        comment = conn.execute("SELECT * FROM task_comments WHERE uid = ?", (comment_uid,)).fetchone()
        if task is None or comment is None or comment["task_id"] != task_id:
            raise ValueError("task/comment identity mismatch")
        state = sync.sync_state(conn)
        if state and (state["mode"] != "primary" or not state["gateway_id"]
                      or task["created_on"] != state["gateway_id"]):
            raise PermissionError("diagnostic correction requires the owning primary gateway")
        if task["status"] not in ("todo", "ready", "blocked", "scheduled", "triage") or task["current_run_id"]:
            raise ValueError("diagnostic correction requires an inactive open task")
        request = {
            "request_id": request_id, "comment_uid": comment_uid, "reason": reason,
            "before": {"body": expected_body, "comment": expected_comment}, "after": after,
        }
        for event in kb.list_events(conn, task_id):
            if event.kind != "diagnostic_corrected":
                continue
            if not isinstance(event.payload, dict):
                raise ValueError("malformed correction audit; no correction applied")
            if event.payload.get("request_id") != request_id:
                continue
            if any(event.payload.get(key) != value for key, value in request.items()):
                raise ValueError("request_id conflict; no correction applied")
            if task["body"] != after["body"] or comment["body"] != after["comment"]:
                raise ValueError("correction state drifted; no replay applied")
            return event.payload
        if task["body"] != expected_body or comment["body"] != expected_comment:
            raise ValueError("stale body or comment; no correction applied")
        payload = {
            "request_id": request_id, "comment_uid": comment_uid, "reason": reason,
            "before": {"body": expected_body, "comment": expected_comment}, "after": after,
            "process_context": {
                "authority_model": "trusted-operator", "worker_markers_present": False,
                "session_hint": os.environ.get("HERMES_SESSION_ID"),
                "gateway": state["gateway_id"] if state else None,
            },
        }
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (after["body"], task_id))
        conn.execute("UPDATE task_comments SET body = ? WHERE uid = ?", (after["comment"], comment_uid))
        kb._append_event(conn, task_id, "diagnostic_corrected", payload)
    return payload


def quote_diagnostic(original: str, diagnostic: str) -> str:
    """Insert quotation marks around an exact diagnostic occurrence."""
    if not diagnostic or any(char in diagnostic for char in ('"', '\n', '\r')):
        raise ValueError("diagnostic must be nonempty single-line text without double quotes")
    if original.count(diagnostic) != 1:
        raise ValueError("diagnostic occurrence is missing or ambiguous")
    start = original.index(diagnostic)
    acceptance = re.search(r"acceptance:", original, re.IGNORECASE)
    if acceptance is not None and start + len(diagnostic) > acceptance.start():
        raise ValueError("diagnostic overlaps acceptance text")
    return original[:start] + f'"{diagnostic}"' + original[start + len(diagnostic):]
