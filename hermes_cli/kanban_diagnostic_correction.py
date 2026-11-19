"""Quote diagnostic prose without rewriting the text it describes. [daveey]

Quotation planning is pure. It grants no authority to mutate a card or comment.
"""
from __future__ import annotations

import os
import sqlite3


def correct_diagnostics(
    conn: sqlite3.Connection, task_id: str, *, comment_uid: str,
    expected_body: str, expected_comment: str, body_diagnostic: str,
    comment_diagnostic: str, reason: str, request_id: str,
) -> dict:
    """Operator-only local CAS; sync convergence must be verified separately.

    Caller-provided prose is an audit reason, never an authorization grant.
    The existing process/DB fence remains the mutation authority boundary.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_review_required import worker_identity

    if worker_identity():
        raise PermissionError("diagnostic correction requires an independent non-worker operator")
    if not reason.strip() or not request_id.strip():
        raise ValueError("reason and request_id must be nonempty")
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
        if task["body"] != expected_body or comment["body"] != expected_comment:
            raise ValueError("stale body or comment; no correction applied")
        payload = {
            "request_id": request_id, "comment_uid": comment_uid, "reason": reason,
            "before": {"body": expected_body, "comment": expected_comment}, "after": after,
            "operator": {"kind": "non-worker", "session": os.environ.get("HERMES_SESSION_ID"),
                         "gateway": state["gateway_id"] if state else None},
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
    acceptance = original.casefold().find("acceptance:")
    if acceptance >= 0 and start + len(diagnostic) > acceptance:
        raise ValueError("diagnostic overlaps acceptance text")
    return original[:start] + f'"{diagnostic}"' + original[start + len(diagnostic):]
