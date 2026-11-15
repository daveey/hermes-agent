"""An applied delivery belongs to its task, not to one executor's ledger."""
import json
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as calls
from hermes_cli import kanban_worker_node as worker
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_continuation import continue_task
from tests.hermes_cli import test_kanban_operator_call as helpers
from tests.hermes_cli import test_kanban_operator_delivery as delivery

fleet = helpers.fleet


def _resume(f, conn):
    assert continue_task(conn, f.caller, checkpoint="receipt retained")["ok"]
    assert kb.claim_task(conn, f.caller)
    f.run_uid(conn, f.caller)
    return kb.get_task(conn, f.caller).current_run_id


def _tool(f, conn, monkeypatch, *, run_id=None, task_id=None):
    from tools import kanban_tools as kt
    task = kb.get_task(conn, f.caller)
    for key, value in {"HERMES_KANBAN_TASK": f.caller,
                       "HERMES_KANBAN_RUN_ID": str(run_id or task.current_run_id),
                       "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock}.items():
        monkeypatch.setenv(key, value)

    @contextmanager
    def board(_name=None):
        yield kb, conn

    monkeypatch.setattr(kt, "_board", board)
    return json.loads(kt._handle_complete({"task_id": task_id or f.caller,
        "summary": f"Posted the findings to {f.target}.", "metadata": delivery._receipt(f.proof)}))


@pytest.mark.parametrize("migrate", [False, True])
def test_complete_tool_accepts_same_task_successor(fleet, tmp_path, monkeypatch, migrate):
    f = fleet
    from hermes_cli import kanban_db_sync as sync
    for source in (f.w, f.h, f.rep):
        sync.apply_sync_schema_steps(source)
    f.proof = delivery._delivered(f)
    original = dict(f.proof)
    if not migrate:
        conn = f.w
        new_run = _resume(f, conn)
    else:
        assert continue_task(f.w, f.caller, checkpoint="receipt retained")["ok"]
        f.board()
        from tests.hermes_cli import kanban_sync_support as support
        from hermes_cli import kanban_sync_client as client
        support.write_sync_config(tmp_path / ".hermes", True)
        monkeypatch.setattr(sync, "_LEASE_CLIENT", support.FakeLeaseClient())
        conn = connect(tmp_path / "worker6.db")
        f.gw[id(conn)] = "worker-6"
        client.ensure_identity(conn, "default", "worker-6")
        sync.set_mode(conn, "primary")
        # Force different local integer identities before real pickup/import.
        for i in range(3):
            tid = f.card(conn, f"padding {i}")
            kb.claim_task(conn, tid)
        assert worker.import_cards(conn, f.rep, [f.caller]) == [f.caller]
        assert not calls._has_log(conn)
        assert kb.claim_task(conn, f.caller)
        f.run_uid(conn, f.caller)
        new_run = kb.get_task(conn, f.caller).current_run_id
        old = conn.execute("SELECT id FROM task_runs WHERE uid=?", (f.proof["caller_run_uid"],)).fetchone()
        assert old[0] != f.run_id
    try:
        assert new_run != f.run_id
        out = _tool(f, conn, monkeypatch)
        assert out.get("ok"), out
        assert kb.get_task(conn, f.caller).status == "done"
        stored = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id=?", (new_run,)).fetchone()[0])
        assert stored["completion_receipt"] == delivery._receipt(original)["completion_receipt"]
        assert f.h.execute("SELECT count(*) FROM task_comments WHERE task_id=?", (f.target,)).fetchone()[0] == 1
    finally:
        if migrate:
            conn.close()


@pytest.mark.parametrize("fault", ["stale_run", "other_task", "foreign_current_run", "ended_run", "nonrunning_run", "missing_uid"])
def test_complete_tool_rejects_foreign_custody(fleet, monkeypatch, fault):
    f = fleet
    f.proof = delivery._delivered(f)
    _resume(f, f.w)
    other = f.card(f.w, "other task")
    kb.claim_task(f.w, other)
    f.run_uid(f.w, other)
    with kb.write_txn(f.w):
        if fault == "foreign_current_run":
            f.w.execute("UPDATE tasks SET current_run_id=? WHERE id=?",
                        (kb.get_task(f.w, other).current_run_id, f.caller))
        if fault == "ended_run":
            f.w.execute("UPDATE task_runs SET ended_at=1 WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", (f.caller,))
        if fault == "nonrunning_run":
            f.w.execute("UPDATE task_runs SET status='continued' WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", (f.caller,))
        if fault == "missing_uid":
            f.w.execute("UPDATE task_runs SET uid=NULL WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", (f.caller,))
    out = _tool(f, f.w, monkeypatch, run_id=f.run_id if fault == "stale_run" else None,
                task_id=other if fault == "other_task" else None)
    assert not out.get("ok"), out
    assert kb.get_task(f.w, f.caller).status == "running"
    assert kb.get_task(f.w, other).status == "running"


@pytest.mark.parametrize("fault", ["missing", "ambiguous", "conflict", "canonical", "pending", "failed"])
def test_carried_request_is_only_discovery(fleet, monkeypatch, fault):
    f = fleet
    f.proof = delivery._delivered(f)
    _resume(f, f.w)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE request_uid=?", (f.proof["request_uid"],)).fetchone()[0]
    with kb.write_txn(f.w):
        f.w.execute("DELETE FROM operator_calls WHERE side='request'")
    if fault == "missing":
        for conn in (f.w, f.rep):
            with kb.write_txn(conn):
                conn.execute("DELETE FROM task_events WHERE uid=?", (uid,))
    elif fault == "ambiguous":
        with kb.write_txn(f.rep):
            f.rep.execute("INSERT INTO task_events(task_id,kind,payload,created_at,uid) SELECT task_id,kind,payload,created_at,'second-uid' FROM task_events WHERE uid=?", (uid,))
    elif fault in ("conflict", "canonical"):
        payload = json.loads(f.canonical[uid]["payload"])
        payload["args"]["text"] = "forged"
        with kb.write_txn(f.rep):
            f.rep.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), uid))
        if fault == "canonical":
            with kb.write_txn(f.w):
                f.w.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), uid))
            import hashlib
            f.proof["content_sha256"] = hashlib.sha256(b"forged").hexdigest()
    else:
        row = f.canonical[f.proof["result_event_uid"]]
        result = json.loads(row["payload"])
        result["outcome"] = fault
        row["payload"] = json.dumps(result)
    out = _tool(f, f.w, monkeypatch)
    assert not out.get("ok"), out
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("event", ["request", "result"])
@pytest.mark.parametrize("field,value", [("origin_gateway", "foreign"), ("task_id", "unrelated"),
    ("kind", "unrelated"), ("uid", "forged"), ("deleted_at", 1)])
def test_ledgerless_canonical_identity(fleet, monkeypatch, event, field, value):
    f = fleet
    f.proof = delivery._delivered(f)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE request_uid=?",
                      (f.proof["request_uid"],)).fetchone()[0]
    _resume(f, f.w)
    with kb.write_txn(f.w):
        f.w.execute("DROP TABLE operator_calls")
    if event == "result":
        uid = f.proof["result_event_uid"]
    if field == "origin_gateway":
        f.origin[uid] = value
    else:
        f.canonical[uid][field] = value
    out = _tool(f, f.w, monkeypatch)
    assert not out.get("ok"), out
    assert kb.get_task(f.w, f.caller).status == "running"


def test_conflicting_local_request_cannot_be_replaced_by_replica(fleet, monkeypatch):
    f = fleet
    f.proof = delivery._delivered(f)
    _resume(f, f.w)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE request_uid=?",
                      (f.proof["request_uid"],)).fetchone()[0]
    # Replica and canonical request remain valid. Ignoring conflict would replace
    # the altered local snapshot with that valid request and admit completion.
    payload = json.loads(f.canonical[uid]["payload"])
    payload["extra_unbound_field"] = "conflicting local copy"
    with kb.write_txn(f.w):
        f.w.execute("DROP TABLE operator_calls")
        f.w.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), uid))
    out = _tool(f, f.w, monkeypatch)
    assert not out.get("ok"), out
    assert "conflicting carried request" in str(out)
    assert kb.get_task(f.w, f.caller).status == "running"


def test_request_snapshot_frozen_across_canonical_read(fleet, monkeypatch):
    f = fleet
    f.proof = delivery._delivered(f)
    _resume(f, f.w)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE request_uid=?",
                      (f.proof["request_uid"],)).fetchone()[0]
    original = calls._evidence

    def evidence(board, event_uid):
        result = original(board, event_uid)
        if event_uid == f.proof["result_event_uid"]:
            payload = json.loads(f.canonical[uid]["payload"])
            payload["extra_unbound_field"] = "changed after authentication"
            with kb.write_txn(f.w):
                f.w.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), uid))
        return result

    monkeypatch.setattr(calls, "_evidence", evidence)
    out = _tool(f, f.w, monkeypatch)
    assert not out.get("ok"), out
    assert "request changed" in str(out)
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("outcome", ["pending", "failed"])
def test_local_ledger_refusal_survives_valid_carried_evidence(fleet, monkeypatch, outcome):
    f = fleet
    f.proof = delivery._delivered(f)
    _resume(f, f.w)
    with kb.write_txn(f.w):
        f.w.execute("UPDATE operator_calls SET outcome=? WHERE side='request'", (outcome,))
    out = _tool(f, f.w, monkeypatch)
    assert not out.get("ok"), out
    assert "pending, failed or missing" in str(out)
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("field", ["caller", "args"])
def test_malformed_carried_envelope_is_controlled_refusal(fleet, monkeypatch, field):
    f = fleet
    f.proof = delivery._delivered(f)
    _resume(f, f.w)
    uid = f.w.execute("SELECT request_event_uid FROM operator_calls WHERE side='request'").fetchone()[0]
    payload = json.loads(f.canonical[uid]["payload"])
    payload[field] = []
    for conn in (f.w, f.rep):
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE uid=?", (json.dumps(payload), uid))
    with kb.write_txn(f.w):
        f.w.execute("DROP TABLE operator_calls")
    out = _tool(f, f.w, monkeypatch)
    assert not out.get("ok"), out
    assert "malformed carried request" in str(out)


@pytest.mark.parametrize("raw,expected", [
    ("not json", False), ("[]", False),
    ('{"request_uid":"wanted","action":"comment","target_task_id":"target"}', True),
    ('{"request_uid":"other","action":"comment","target_task_id":"target"}', False),
])
def test_request_discovery_filters(fleet, raw, expected):
    from hermes_cli.kanban_operator_delivery_custody import request_events
    f = fleet
    with kb.write_txn(f.w):
        f.w.execute("INSERT INTO task_events(task_id,kind,payload,created_at,uid) VALUES (?,?,?,1,'event')",
                    (f.caller, calls.REQUEST_KIND, raw))
    assert bool(request_events(f.w, f.caller, "wanted")) is expected
    assert request_events(None, f.caller, "wanted") == {}


@pytest.mark.parametrize("raw,expected", [
    ("not json", set()), ("[]", set()),
    ('{"action":"comment","target_task_id":"target"}', {"target"}),
    ('{"action":"move","target_task_id":"target"}', set()),
    ('{"action":"comment","target_task_id":[]}', set()),
])
def test_history_gating_filters(fleet, raw, expected):
    from hermes_cli.kanban_operator_delivery_custody import requested_targets
    f = fleet
    with kb.write_txn(f.w):
        f.w.execute("INSERT INTO task_events(task_id,kind,payload,created_at,uid) VALUES (?,?,?,1,'event')",
                    (f.caller, calls.REQUEST_KIND, raw))
    assert requested_targets(f.w, f.caller) == expected
    assert requested_targets(None, f.caller) == set()


def test_carried_request_requires_event_uid(fleet, monkeypatch):
    from hermes_cli.kanban_operator_delivery_custody import carried_request
    from hermes_cli import kanban_operator_delivery_custody as custody
    monkeypatch.setattr(custody, "request_events", lambda *args: {None: {"caller": {}, "args": {}}})
    with pytest.raises(ValueError, match="unique carried request missing"):
        carried_request(fleet.w, fleet.caller, {"request_uid": "wanted"})


def test_current_run_is_frozen_across_canonical_read(fleet, monkeypatch):
    f = fleet
    f.proof = delivery._delivered(f)
    original = calls._evidence
    switched = []

    def evidence(board, uid):
        result = original(board, uid)
        if not switched:
            switched.append(_resume(f, f.w))
        return result

    monkeypatch.setattr(calls, "_evidence", evidence)
    with pytest.raises(ValueError, match="current run changed"):
        kb.complete_task(f.w, f.caller, metadata=delivery._receipt(f.proof), summary="Verified delivery")
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("local_event", [False, True])
def test_unavailable_replica_is_controlled_refusal(fleet, monkeypatch, local_event):
    f = fleet
    f.proof = delivery._delivered(f)
    _resume(f, f.w)
    with kb.write_txn(f.w):
        f.w.execute("DELETE FROM operator_calls")
        if not local_event:
            f.w.execute("DELETE FROM task_events WHERE kind=?", (calls.REQUEST_KIND,))

    @contextmanager
    def unavailable(_conn):
        yield None

    monkeypatch.setattr(calls, "_replica", unavailable)
    out = _tool(f, f.w, monkeypatch)
    assert not out.get("ok"), out
    assert kb.get_task(f.w, f.caller).status == "running"


@pytest.mark.parametrize("source", ["local", "replica"])
def test_carried_request_requires_receipt_even_with_local_target(fleet, source):
    f = fleet
    delivery._delivered(f)
    _resume(f, f.w)
    with kb.write_txn(f.w):
        f.w.execute("DELETE FROM operator_calls")
        f.w.execute("DELETE FROM supervisor_requests")
        target = f.h.execute("SELECT * FROM tasks WHERE id=?", (f.target,)).fetchone()
        f.w.execute(f"INSERT INTO tasks ({','.join(target.keys())}) VALUES ({','.join('?' for _ in target)})", tuple(target))
    absent = f.rep if source == "local" else f.w
    with kb.write_txn(absent):
        absent.execute("DELETE FROM task_events WHERE kind=?", (calls.REQUEST_KIND,))
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(f.w, f.caller, summary=f"Posted findings to {f.target}.")
    assert kb.get_task(f.w, f.caller).status == "running"
