"""Locate a moved caller's request; the replica is a locator, never authority."""
from __future__ import annotations

import json


def moved_request(conn, task_id, proof):
    """Recover only immutable request identity, without writing a replacement log.

    Operator ledgers are gateway-local. A destination can locate the original
    request in the Board feed; verify() still authenticates both that exact
    event and its applied result against the canonical Board before admission.
    This function also runs inside the completion transaction: no network I/O.
    """
    from hermes_cli import kanban_operator_call as calls
    from hermes_cli.kanban_operator_delivery import _bind_request

    if proof["caller_task_id"] != task_id or calls._board_slug(conn) != proof["board"]:
        raise ValueError("completion_receipt: operator caller/board mismatch")
    matches = []
    with calls._replica(conn) as rep:
        if rep is None:
            raise ValueError("completion_receipt: original operator request replica unavailable")
        for event in rep.execute("SELECT uid, payload FROM task_events WHERE task_id=? AND kind=?",
                                 (task_id, calls.REQUEST_KIND)):
            request = json.loads(event["payload"])
            if isinstance(request, dict) and request.get("request_uid") == proof["request_uid"]:
                matches.append((event["uid"], request))
    if len(matches) != 1 or not matches[0][0]:
        raise ValueError("completion_receipt: original operator request missing or ambiguous")
    uid, request = matches[0]
    args = request.get("args") or {}
    row = {"request_event_uid": uid, "caller_gateway": proof["caller_gateway"],
           "args_digest": calls._digest({"action": "comment", "task_id": proof["target_task_id"], **args})}
    _bind_request(request, row, proof)
    return row, request
