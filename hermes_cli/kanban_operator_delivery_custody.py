"""Task-bound discovery for an operator receipt after a worker continuation.

Carried events are locators, not authority. The delivery verifier authenticates
both complete payloads against the Board before admitting a receipt. No request
is recreated and the original caller identity is never rewritten.
"""
from __future__ import annotations

import json


def current_run(conn, task_id):
    row = conn.execute(
        "SELECT r.uid FROM task_runs r JOIN tasks t ON t.current_run_id=r.id "
        "AND r.task_id=t.id WHERE t.id=? AND r.status='running' AND r.ended_at IS NULL",
        (task_id,)).fetchone()
    if row is None or not row[0]:
        raise ValueError("completion_receipt: operator current task/run unavailable")
    return row[0]


def request_events(conn, task_id, request_uid):
    from hermes_cli import kanban_operator_call as calls
    found = {}
    if conn is None:
        return found
    for event in conn.execute(
            "SELECT uid, payload FROM task_events WHERE task_id=? AND kind=?",
            (task_id, calls.REQUEST_KIND)):
        try:
            payload = json.loads(event[1])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("request_uid") == request_uid:
            found[event[0]] = payload
    return found


def requested_targets(conn, task_id):
    from hermes_cli import kanban_operator_call as calls
    targets = set()
    if conn is None:
        return targets
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                               (task_id, calls.REQUEST_KIND)):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (isinstance(payload, dict) and payload.get("action") == "comment"
                and isinstance(payload.get("target_task_id"), str)):
            targets.add(payload["target_task_id"])
    return targets


def carried_request(conn, task_id, proof):
    """Locate a unique request by portable UID; replica contents prove nothing."""
    from hermes_cli import kanban_operator_call as calls
    found = request_events(conn, task_id, proof["request_uid"])
    with calls._replica(conn) as rep:
        for uid, payload in request_events(rep, task_id, proof["request_uid"]).items():
            if uid in found and found[uid] != payload:
                raise ValueError("completion_receipt: conflicting carried request")
            found[uid] = payload
    if len(found) != 1 or not next(iter(found)):
        raise ValueError("completion_receipt: unique carried request missing")
    uid, request = next(iter(found.items()))
    caller, args = request.get("caller"), request.get("args")
    if not isinstance(caller, dict) or not isinstance(args, dict):
        raise ValueError("completion_receipt: malformed carried request")
    # This is only a verifier input, never an inserted operator_calls row.
    row = {"request_event_uid": uid, "caller_task_id": caller.get("task_id"),
           "caller_run_uid": caller.get("run_uid"), "caller_gateway": caller.get("gateway"),
           "action": request.get("action"), "target_task_id": request.get("target_task_id"),
           "board": request.get("board"),
           "args_digest": calls._digest({"action": request.get("action"),
                                         "task_id": request.get("target_task_id"), **args})}
    return row, request
