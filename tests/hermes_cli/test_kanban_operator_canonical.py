"""The Board payload, not an origin-only attestation, authorizes relay."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as calls
from hermes_cli import kanban_supervisor_requests as requests
from tests.hermes_cli import test_kanban_operator_call as helpers

fleet = helpers.fleet


@pytest.mark.parametrize("side", ["request", "result"])
@pytest.mark.parametrize("field,value", [
    ("state", "deleted"), ("table", "task_comments"), ("uid", "other"),
    ("row.uid", "other"), ("row.task_id", "other"), ("row.kind", "commented"),
    ("row.origin_gateway", "impostor"), ("row.deleted_at", 1),
    ("row.payload", "[]"), ("row.payload", "{"),
])
def test_transport_requires_exact_canonical_envelope(fleet, monkeypatch, side, field, value):
    f = fleet
    submitted = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "approved"})
    requests.drain(f.w)
    f.board()
    if side == "result":
        requests.drain(f.h)
        f.board()
    kind = calls.REQUEST_KIND if side == "request" else calls.RESULT_KIND
    uid = f.rep.execute("SELECT uid FROM task_events WHERE kind=?", (kind,)).fetchone()[0]
    original = calls._evidence

    def evidence(board, event_uid):
        result = original(board, event_uid)
        if event_uid == uid:
            if field.startswith("row."):
                result["row"][field[4:]] = value
            else:
                result[field] = value
        return result

    monkeypatch.setattr(calls, "_evidence", evidence)
    if side == "request":
        requests.drain(f.h)
        assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id=?", (f.target,)).fetchone()
        f.board()
    requests.drain(f.w)
    request = requests.get_request(f.w, submitted["request_id"])
    assert request["status"] == ("rejected" if side == "request" else "accepted")


def test_changed_replica_request_never_executes(fleet):
    f = fleet
    submitted = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "approved"})
    requests.drain(f.w)
    f.board()
    event = f.rep.execute("SELECT uid,payload FROM task_events WHERE kind=?", (calls.REQUEST_KIND,)).fetchone()
    payload = json.loads(event[1])
    payload["args"]["text"] = "not approved"
    with kb.write_txn(f.rep):
        f.rep.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), event[0]))
    requests.drain(f.h)
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id=?", (f.target,)).fetchone()
    f.board()
    requests.drain(f.w)
    assert requests.get_request(f.w, submitted["request_id"])["status"] == "rejected"


def test_changed_replica_result_cannot_finish_call(fleet):
    f = fleet
    submitted = helpers._submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "approved"})
    requests.drain(f.w)
    f.board()
    requests.drain(f.h)
    f.board()
    event = f.rep.execute("SELECT uid,payload FROM task_events WHERE kind=?", (calls.RESULT_KIND,)).fetchone()
    payload = json.loads(event[1])
    payload["detail"] = "fabricated receipt detail"
    with kb.write_txn(f.rep):
        f.rep.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), event[0]))
    requests.drain(f.w)
    assert requests.get_request(f.w, submitted["request_id"])["status"] == "accepted"
