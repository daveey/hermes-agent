"""Isolated recovery boundaries from mutation-plan.md; no synthetic receipt inputs."""
import json

import pytest

from hermes_cli import kanban_sync_reserved_key as rk
from hermes_cli.kanban_sync_rejections import resolve_rejections
from tests.hermes_cli import test_kanban_sync_reserved_key as base
from tests.hermes_cli import test_kanban_sync_reserved_key_safety as safety

# Register the existing fixtures without importing their test functions into this module.
kanban_home = base.kanban_home
svc = base.svc
live = base.live
clock = safety.clock


def _op(conn, op_id):
    row = conn.execute("SELECT * FROM kanban_sync_outbox WHERE op_id=?", (op_id,)).fetchone()
    return dict(row) if row else None


def _reinsert(conn, row, state="pending"):
    """Restore a real previously queued op, retaining its tracked identity and payload."""
    row = {**row, "state": state}
    columns = list(row)
    with base.kbc.write_txn(conn):
        conn.execute(
            f"INSERT INTO kanban_sync_outbox ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})", [row[c] for c in columns])
    return row


def _delivered_children(conn, service, tid):
    """Stop just before finish, with every manifest row delivered and no tracked op left."""
    safety.accepted_parent(conn, service, tid)
    queued = rk.advance(conn, service.hub(base.ME), base.BOARD)[0]
    assert queued["advanced"] and queued["children_queued"] > 0
    children = [dict(r) for r in conn.execute(
        "SELECT o.* FROM kanban_sync_outbox o JOIN kanban_sync_key_supersession_children c "
        "ON c.op_id=o.op_id ORDER BY o.op_id")]
    assert children
    base.client.push(conn, service.hub(base.ME), base.BOARD, base.ME, primary=False)
    assert all(_op(conn, child["op_id"]) is None for child in children)
    for child in children:
        remote = service.board(base.BOARD)["rows"][child["table_name"]][child["row_uid"]]
        assert remote["uid"] == child["row_uid"] and not remote.get("deleted_at")
    _assert_unfinished(conn, tid)
    return children


def _assert_unfinished(conn, tid):
    receipt = rk.receipt_for_task(conn, tid)
    assert receipt["state"] == "children_queued"
    assert receipt["completed_at"] is None


def _assert_parent_waiting(conn, tid):
    assert rk.receipt_for_task(conn, tid)["state"] == "parent_queued"
    assert conn.execute("SELECT count(*) FROM kanban_sync_key_supersession_children").fetchone()[0] == 0
    assert not conn.execute(
        "SELECT 1 FROM kanban_sync_outbox WHERE state='pending' AND table_name!='tasks'").fetchone()


def _assert_retained(conn, service, original_ops):
    reports = {r["op_id"]: r for r in resolve_rejections(conn, service.hub(base.ME), base.BOARD)}
    for original in original_ops:
        report = reports[original["op_id"]]
        assert report["classification"] == "unresolved", report
        assert report["action"] == "retained", report
        assert _op(conn, original["op_id"]) == original


def test_file_gateway_only_tamper_retains_task_and_child_bytes(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    old = base.rejected_ops(live)
    task = next(o for o in old if o["op_id"] == base.key_op(live))
    comment = next(o for o in old if o["table_name"] == "task_comments")
    safety.completed(live, svc, tid)
    base.quiet(live, "UPDATE kanban_sync_state SET gateway_id=?", (base.OTHER,))
    receipt = rk.receipt_for_task(live, tid)
    assert receipt["gateway_id"] == base.ME
    assert svc.hub(base.ME).ident.gateway == base.ME
    assert svc.board(base.BOARD)["rows"]["tasks"][tid]["created_on"] == base.ME
    _assert_retained(live, svc, [task, comment])


@pytest.mark.parametrize("column,value", [("table_name", "task_events"), ("row_uid", "another-comment")])
def test_receipt_identity_metadata_custody_without_byte_changes(live, svc, monkeypatch, column, value):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    comment = next(o for o in base.rejected_ops(live) if o["table_name"] == "task_comments")
    safety.completed(live, svc, tid)
    base.quiet(live, f"UPDATE kanban_sync_key_supersession_ops SET {column}=? WHERE old_op_id=?",
               (value, comment["op_id"]))
    _assert_retained(live, svc, [comment])


def test_child_removed_only_from_receipt_manifest_cannot_retire(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    comment = next(o for o in base.rejected_ops(live) if o["table_name"] == "task_comments")
    safety.completed(live, svc, tid)
    receipt = rk.receipt_for_task(live, tid)
    identity = ["task_comments", comment["row_uid"]]
    children = json.loads(receipt["children"])
    assert identity in children
    children.remove(identity)
    base.quiet(live, "UPDATE kanban_sync_key_supersessions SET children=? WHERE receipt_id=?",
               (json.dumps(children), receipt["receipt_id"]))
    assert rk.receipt_for_op(live, comment["op_id"]) is not None
    _assert_retained(live, svc, [comment])


def test_child_retirement_checks_corrected_remote_parent_key(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    comment = next(o for o in base.rejected_ops(live) if o["table_name"] == "task_comments")
    safety.completed(live, svc, tid)
    svc.board(base.BOARD)["rows"]["tasks"][tid]["idempotency_key"] = "another-key"
    _assert_retained(live, svc, [comment])


@pytest.mark.parametrize("column", ["claim_expires", "worker_started_at", "lease_expires", "lease_version"])
def test_each_active_column_alone_refuses_without_writes(live, svc, monkeypatch, column):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    active = ("claim_lock", "claim_expires", "worker_pid", "worker_started_at", "lease_holder",
              "lease_expires", "lease_version", "current_run_id")
    row = live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert all(row[c] is None for c in active)
    assert base.preview(live, svc, tid)["eligible"]
    base.quiet(live, f"UPDATE tasks SET {column}=? WHERE id=?", (1, tid))
    base.assert_refused(live, svc, tid, "active_run_or_lease")


@pytest.mark.parametrize("contradiction", [{"floor": 1}, {"row": {"uid": "contradictory-live-row"}}],
                         ids=["positive-floor", "nonnull-row"])
def test_never_existed_requires_zero_floor_and_no_row(live, svc, monkeypatch, contradiction):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    token = base.preview(live, svc, tid)["token"]
    hub = svc.hub(base.ME)
    original = hub.row_evidence

    def contradictory(*args):
        evidence = original(*args)
        assert evidence["state"] == "never_existed"
        return {**evidence, **contradiction}

    monkeypatch.setattr(hub, "row_evidence", contradictory)
    before = base.dump(live)
    report = rk.plan(live, hub, base.BOARD, tid, base.GOOD, base.REASON)
    assert not report["eligible"]
    assert report["refusal"].startswith("board_history_not_proven_never_existed")
    result = rk.apply(live, hub, base.BOARD, tid, base.GOOD, token, base.REASON)
    assert result["action"] == "refused"
    assert base.dump(live) == before


def test_finish_writer_wait_expires_without_completion_or_writes(live, svc, monkeypatch, clock):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    _delivered_children(live, svc, tid)
    safety.slow_lock(monkeypatch, clock, rk.EVIDENCE_MAX_AGE_S + 1)
    before = base.dump(live)
    report = rk.advance(live, svc.hub(base.ME), base.BOARD)[0]
    assert not report["advanced"] and report["waiting"] == "evidence_interval_exceeded"
    _assert_unfinished(live, tid)
    assert base.dump(live) == before


@pytest.mark.parametrize("race", ["board_identity", "child_outbox"])
def test_finish_rechecks_identity_and_child_ops_after_delivery_evidence(live, svc, monkeypatch, race):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    children = _delivered_children(live, svc, tid)
    hub = svc.hub(base.ME)
    original = hub.row_evidence
    after_race = []

    def read(*args):
        evidence = original(*args)
        if not after_race:
            assert evidence["state"] == "live"
            if race == "board_identity":
                base.quiet(live, "UPDATE kanban_sync_state SET board_id='moved-board'")
            else:
                _reinsert(live, children[0])
            after_race.append(base.dump(live))
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    report = rk.advance(live, hub, base.BOARD)[0]
    assert after_race, "the race must occur during finish evidence, not at its entry veto"
    assert not report["advanced"] and "changed during evidence read" in report["waiting"]
    _assert_unfinished(live, tid)
    assert base.dump(live) == after_race[0]


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_acknowledged_parent_reinsert_blocks_children(live, svc, monkeypatch, state):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    assert base.apply(live, svc, tid, base.preview(live, svc, tid)["token"])["action"] == "applied"
    parent = _op(live, rk.receipt_for_task(live, tid)["parent_op_id"])
    base.client.push(live, svc.hub(base.ME), base.BOARD, base.ME, primary=False)
    base.client._pull_replica(base.BOARD, svc.hub(base.ME), base.ME)
    assert live.execute("SELECT sync_version FROM tasks WHERE id=?", (tid,)).fetchone()[0] is not None
    assert svc.board(base.BOARD)["rows"]["tasks"][tid]["idempotency_key"] == base.GOOD
    restored = _reinsert(live, parent, state)
    before = base.dump(live)
    report = rk.advance(live, svc.hub(base.ME), base.BOARD)[0]
    assert not report["advanced"] and report["waiting"] == f"parent op {state}"
    _assert_parent_waiting(live, tid)
    assert _op(live, parent["op_id"]) == restored
    assert base.dump(live) == before


@pytest.mark.parametrize("race", ["parent_outbox", "parent_key", "lost_ack"])
def test_queue_rechecks_parent_after_remote_acceptance_read(live, svc, monkeypatch, race):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    assert base.apply(live, svc, tid, base.preview(live, svc, tid)["token"])["action"] == "applied"
    parent = _op(live, rk.receipt_for_task(live, tid)["parent_op_id"])
    base.client.push(live, svc.hub(base.ME), base.BOARD, base.ME, primary=False)
    base.client._pull_replica(base.BOARD, svc.hub(base.ME), base.ME)
    hub = svc.hub(base.ME)
    original = hub.row_evidence
    after_race = []

    def read(*args):
        evidence = original(*args)
        if not after_race:
            assert evidence["state"] == "live"
            if race == "parent_outbox":
                _reinsert(live, parent)
            elif race == "parent_key":
                base.quiet(live, "UPDATE tasks SET idempotency_key='racing-key' WHERE id=?", (tid,))
            else:
                base.quiet(live, "UPDATE tasks SET sync_version=NULL WHERE id=?", (tid,))
            after_race.append(base.dump(live))
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    report = rk.advance(live, hub, base.BOARD)[0]
    assert after_race and not report["advanced"]
    assert "changed during evidence read" in report["waiting"]
    _assert_parent_waiting(live, tid)
    assert base.dump(live) == after_race[0]


def test_parent_creator_is_checked_before_queueing_children(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    safety.accepted_parent(live, svc, tid)
    svc.board(base.BOARD)["rows"]["tasks"][tid]["created_on"] = base.OTHER
    before = base.dump(live)
    report = rk.advance(live, svc.hub(base.ME), base.BOARD)[0]
    assert not report["advanced"] and report["waiting"] == "Board has not accepted the corrected card"
    _assert_parent_waiting(live, tid)
    assert base.dump(live) == before


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_delivered_child_reinsert_still_blocks_completion(live, svc, monkeypatch, state):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    children = _delivered_children(live, svc, tid)
    restored = _reinsert(live, children[0], state)
    before = base.dump(live)
    report = rk.advance(live, svc.hub(base.ME), base.BOARD)[0]
    assert not report["advanced"]
    assert ("rejected" if state == "rejected" else "still queued (pending)") in report["waiting"]
    _assert_unfinished(live, tid)
    assert _op(live, restored["op_id"]) == restored
    assert base.dump(live) == before


def test_queued_child_forbidden_field_never_downgrades_approved_payload(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    safety.accepted_parent(live, svc, tid)
    assert rk.advance(live, svc.hub(base.ME), base.BOARD)[0]["advanced"]
    child = dict(live.execute(
        "SELECT o.* FROM kanban_sync_outbox o JOIN kanban_sync_key_supersession_children c "
        "ON c.op_id=o.op_id WHERE o.table_name='task_comments' LIMIT 1").fetchone())
    assert "body" in json.loads(child["fields"])
    hub = svc.hub(base.ME)
    original = hub.ops
    sent = []

    def refuse_child(board, ops):
        sent.extend(ops)
        results = []
        for op in ops:
            if op["client_op_id"] == child["op_id"]:
                assert "body" in op["fields"]
                results.append({"client_op_id": op["client_op_id"], "status": "rejected",
                                "reason": "forbidden_field", "detail": "not writable on task_comments: body"})
            else:
                results.extend(original(board, [op])["results"])
        return {"results": results}

    monkeypatch.setattr(hub, "ops", refuse_child)
    base.client.push(live, hub, base.BOARD, base.ME, primary=False, max_batches=1)
    assert any(op["client_op_id"] == child["op_id"] for op in sent)
    kept = _op(live, child["op_id"])
    assert kept["state"] == "rejected" and kept["fields"] == child["fields"]
    assert live.execute("SELECT count(*) FROM kanban_sync_omissions").fetchone()[0] == 0
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op IN ('compat','compat_import')").fetchone()
    assert live.execute("SELECT op_id FROM kanban_sync_key_supersession_children WHERE op_id=?",
                        (child["op_id"],)).fetchone()[0] == child["op_id"]
    report = rk.advance(live, svc.hub(base.ME), base.BOARD)[0]
    assert not report["advanced"] and "rejected" in report["waiting"]
    _assert_unfinished(live, tid)
