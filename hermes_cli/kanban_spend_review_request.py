"""Local durable outbox for a single immutable caused-stop decision. [daveey]

This is not sync authority: only authenticated Board receipt readback settles it.
Keep completed requests too, so no later invocation can replace their bytes.
"""
from __future__ import annotations

import json

TABLE = "spend_review_requests"
# Separate from judge retries: allow receipt recovery after the model budget ends,
# but never monopolize reviewer custody indefinitely when no receipt is reachable.
MAX_RECONCILE_ATTEMPTS = 4


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def requests(conn):
    if not exists(conn):
        return []
    return [json.loads(row[0]) for row in conn.execute(f"SELECT request FROM {TABLE}")]


def get(conn, task, episode):
    if not exists(conn):
        return None
    row = conn.execute(f"SELECT request FROM {TABLE} WHERE task=? AND episode=?", (task, episode)).fetchone()
    return json.loads(row[0]) if row else None


def freeze(conn, board, gateway, wire):
    from hermes_cli import kanban_db as kb
    # Network submission must never precede the outermost durable COMMIT.
    if conn.in_transaction:
        raise ValueError("cannot freeze a review inside another transaction")
    with kb.write_txn(conn):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (task TEXT NOT NULL, episode TEXT NOT NULL, "
                     "request TEXT NOT NULL, PRIMARY KEY(task,episode))")
        request = {"board": board, "gateway": gateway, "wire": wire}
        conn.execute(f"INSERT OR IGNORE INTO {TABLE} VALUES (?,?,?)",
                     (wire["task"], wire["episode"], json.dumps(request, ensure_ascii=False)))
        frozen = get(conn, wire["task"], wire["episode"])
        assert frozen is not None
        return frozen


def escalate_unconfirmed(conn, task, episode):
    """Transfer exhausted automation to an administrative owner, never cancel it.

    Keep the frozen bytes and all execution holds. This also covers terminal or
    superseded source cards, for which ordinary stop escalation is a no-op.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_reviewer as reviewer
    with kb.write_txn(conn):
        request = get(conn, task, episode)
        if request is None or reviewer.state(conn, task, episode) not in (reviewer.QUEUED, reviewer.STARTED):
            return
        uid = request["wire"]["event_uid"]
        source = kb.get_task(conn, task)
        reason = "Frozen spend review remains unconfirmed after bounded reconciliation"
        body = (f"{reason}. Source card: {task}; stop: {episode}; request: {uid}; "
                f"Board: {request['board']}; gateway: {request['gateway']}.\n\n"
                "Automation has stopped; this is not cancellation or proof that the request was never submitted. "
                "An absent receipt can still race an in-flight operation. The immutable request journal is retained.\n"
                "Reconcile the exact authenticated Board receipt and report its disposition to the owner. "
                "Do not resubmit, choose a replacement verdict, grant runs, or promote the source card. "
                "Preserve human, terminal, scope and safety holds; safety refusals must never be retried. "
                "If no authoritative receipt is reachable, escalate to the Board administrator; do not infer clearance.")
        notice = kb.create_task(conn, title=f"Reconcile unconfirmed spend review on {task}", body=body,
                                assignee="default", created_by=reviewer.AUTHOR, workspace_kind="scratch",
                                tenant=source.tenant if source else None,
                                idempotency_key=f"spend-review-unconfirmed:{task}:{episode}")
        kb._append_event(conn, task, reviewer.ESCALATED,
                         {"episode": episode, "reason": reason, "task_id": notice,
                          "request_uid": uid, "unconfirmed": True})
        kb.add_comment(conn, task, reviewer.AUTHOR,
                       f"{reason}; administrative reconciliation: {notice}. "
                       "Request custody and execution holds retained; no cancellation or release inferred.", system=True)


def refuse(conn, task, episode, reason):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        request = get(conn, task, episode)
        assert request is not None
        request["refused"] = reason
        conn.execute(f"UPDATE {TABLE} SET request=? WHERE task=? AND episode=?",
                     (json.dumps(request, ensure_ascii=False), task, episode))
