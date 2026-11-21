"""A newer core can publish a card to an older Board without losing local fields."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_run_rows as helpers

kanban_home = helpers.kanban_home
svc = helpers.svc
live = helpers.live


@pytest.mark.parametrize("synthetic_parent", [False, True])
def test_forbidden_column_retries_once_with_fresh_identity_and_preserves_local_value(live, svc, synthetic_parent):
    tid = kb.create_task(live, title="new core card", assignee="worker")
    live.execute("UPDATE tasks SET model_pin_mandatory=1 WHERE id=?", (tid,))
    if synthetic_parent:
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name='tasks'")
    live.commit()
    calls = []
    hub = svc.hub("macbook")
    def ops(board, batch):
        calls.extend(copy.deepcopy(batch))
        results = []
        for op in batch:
            if op["table"] == "tasks" and "model_pin_mandatory" in op.get("fields", {}):
                results.append({"client_op_id": op["client_op_id"], "status": "rejected",
                                "reason": "forbidden_field", "detail": "not writable on tasks: model_pin_mandatory"})
            else:
                results.extend(hub.ops(board, [op])["results"])
        return {"results": results}
    compat_hub = SimpleNamespace(ops=ops)
    for _ in range(3):
        client.push(live, compat_hub, "default", "macbook", primary=True)
    task_ops = [op for op in calls if op["table"] == "tasks"]
    stripped = [op for op in task_ops if "model_pin_mandatory" not in op["fields"]]
    assert stripped, "rejected task must be resent without the unsupported column"
    assert not {op["client_op_id"] for op in stripped} & {op["client_op_id"] for op in task_ops if op not in stripped}
    assert tid in svc.board("default")["rows"]["tasks"]
    assert live.execute("SELECT model_pin_mandatory FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 1
    reasons = [json.loads(r[0])["reason"] for r in live.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='sync_rejected'", (tid,))]
    assert any("model_pin_mandatory" in reason for reason in reasons)


@pytest.mark.parametrize("kind", ["compat", "compat_import"])
def test_compat_snapshot_does_not_replay_acknowledged_placement(live, kind):
    tid = kb.create_task(live, title="assigned elsewhere", assignee="worker")
    live.execute("UPDATE tasks SET sync_version=10 WHERE id=?", (tid,))
    row = {"op": kind, "table_name": "tasks", "row_uid": tid,
           "fields": {"title": "assigned elsewhere", "placement_policy": "worker-pool", "runs_on": '["old"]'}}
    fields = client._full_row_if_unseen(live, row)["fields"]
    assert "runs_on" not in fields and "placement_policy" not in fields
    assert "model_pin_mandatory" not in fields  # no rehydration


@pytest.mark.parametrize("reason,detail,state", [
    ("live_claim", "held by other", "rejected"),
    ("not_found", "no task missing", "pending"),
    ("storage_error", "busy", "pending"),
    ("forbidden_field", "not writable on other: model_pin_mandatory", "rejected"),
    ("forbidden_field", "not writable on tasks: absent", "rejected"),
    ("forbidden_field", "not writable on tasks: model_pin_mandatory, priority", "rejected"),
])
def test_other_rejections_do_not_get_compatibility_retries(live, reason, detail, state):
    tid = kb.create_task(live, title="refused", assignee="worker")
    live.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='tasks'")
    live.commit()
    def ops(board, batch):
        return {"results": [{"client_op_id": op["client_op_id"], "status": "rejected",
                             "reason": reason, "detail": detail} for op in batch]}
    client.push(live, SimpleNamespace(ops=ops), "default", "macbook", primary=True, max_batches=1)
    rows = live.execute("SELECT op,state FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (tid,)).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("upsert", state)]


@pytest.mark.parametrize("reason", ["live_claim", "not_found", "storage_error"])
def test_synthetic_parent_non_schema_rejections_use_bounded_outbox_handling(live, reason):
    kb.create_task(live, title="synthetic parent", assignee="worker")
    live.execute("DELETE FROM kanban_sync_outbox WHERE table_name='tasks'")
    live.commit()
    def ops(board, batch):
        return {"results": [{"client_op_id": op["client_op_id"], "status": "rejected",
                             "reason": reason, "detail": "no task missing"} for op in batch]}
    client.push(live, SimpleNamespace(ops=ops), "default", "macbook", primary=True, max_batches=1)
    [parent] = live.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='tasks'").fetchall()
    assert parent["attempts"] == 1
    assert parent["state"] == ("rejected" if reason == "live_claim" else "pending")


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_ordinary_parent_outside_batch_does_not_suppress_prepend(live, svc, state):
    tid = kb.create_task(live, title="parent outside batch", assignee="worker")
    live.execute("UPDATE kanban_sync_outbox SET state=?,retry_after=9999999999 WHERE table_name='tasks'", (state,))
    live.commit()
    calls = []
    hub = svc.hub("macbook")
    def ops(board, batch):
        calls.extend(copy.deepcopy(batch))
        return hub.ops(board, batch)
    client.push(live, SimpleNamespace(ops=ops), "default", "macbook", primary=True, max_batches=1)
    assert calls[0]["table"] == "tasks" and calls[0]["uid"] == tid
    assert tid in svc.board("default")["rows"]["tasks"]


@pytest.mark.parametrize("status,version,expected", [
    ("applied", 12, 12), ("merged", 12, 12), ("applied", 3, 7),
    ("applied", None, 7), ("rejected", 12, 7),
])
def test_synthetic_parent_acknowledgement_is_successful_version_only(live, status, version, expected):
    from hermes_cli import kanban_sync_compat as compat
    tid = kb.create_task(live, title="parent acknowledgement", assignee="worker")
    live.execute("UPDATE tasks SET sync_version=7 WHERE id=?", (tid,))
    result = {"client_op_id": "parent-op", "status": status}
    if version is not None:
        result["version"] = version
    parent = {"client_op_id": "parent-op", "fields": {"title": "parent acknowledgement"},
              "hlc": "0000000000001-0000-macbook"}
    queued = compat.rejected_parent(live, [(parent, tid)], result)
    assert (queued is not None) == (status == "rejected")
    assert live.execute("SELECT sync_version FROM tasks WHERE id=?", (tid,)).fetchone()[0] == expected


@pytest.mark.parametrize("kind", ["created", "sync_rejected"])
def test_child_rejection_records_payload_but_diagnostic_rejection_does_not_recurse(live, kind):
    from hermes_cli import kanban_sync_compat as compat
    tid = kb.create_task(live, title="diagnostic custody", assignee="worker")
    fields = {"task_id": tid, "kind": kind, "payload": "{}"}
    row = {"op_id": "rejected-child", "table_name": "task_events", "row_uid": "event-uid",
           "fields": json.dumps(fields)}
    compat.record_rejection(live, row, "forbidden_field: unsupported")
    events = live.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='sync_rejected'", (tid,)).fetchall()
    if kind == "sync_rejected":
        assert not events
    else:
        assert [json.loads(event[0]) for event in events] == [{
            "reason": "forbidden_field: unsupported", "fields": sorted(fields), "op_id": "rejected-child"}]


@pytest.mark.parametrize("importing", [False, True])
def test_compat_retry_survives_reconnect_and_transport_loss_without_rehydration(live, svc, kanban_home, importing):
    from hermes_cli import kanban_db_connect as kbc
    tid = kb.create_task(live, title="durable downgrade", assignee="worker")
    live.execute("UPDATE tasks SET model_pin_mandatory=1 WHERE id=?", (tid,))
    live.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='tasks' OR rowid NOT IN "
                 "(SELECT MIN(rowid) FROM kanban_sync_outbox WHERE table_name='tasks')")
    if importing:
        live.execute("UPDATE kanban_sync_outbox SET op='import'")
    live.commit()
    calls = []
    hub = svc.hub("macbook")
    lost = False
    def ops(board, batch):
        nonlocal lost
        results = []
        for op in batch:
            if op["table"] == "tasks":
                calls.append(copy.deepcopy(op))
                if "model_pin_mandatory" in op["fields"]:
                    results.append({"client_op_id": op["client_op_id"], "status": "rejected",
                                    "reason": "forbidden_field", "detail": "not writable on tasks: model_pin_mandatory"})
                    continue
                if not lost:
                    lost = True
                    raise OSError("reply unavailable")
            results.extend(hub.ops(board, [op])["results"])
        return {"results": results}
    compat_hub = SimpleNamespace(ops=ops)
    client.push(live, compat_hub, "default", "macbook", primary=True, max_batches=1)
    reconnect = kbc.connect(kanban_home / "kanban.db")
    try:
        with pytest.raises(OSError, match="reply unavailable"):
            client.push(reconnect, compat_hub, "default", "macbook", primary=True, max_batches=1)
    finally:
        reconnect.close()
    reconnect = kbc.connect(kanban_home / "kanban.db")
    try:
        client.push(reconnect, compat_hub, "default", "macbook", primary=True, max_batches=1)
        assert reconnect.execute("SELECT model_pin_mandatory FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 1
    finally:
        reconnect.close()
    assert len(calls) == 3
    assert calls[0]["client_op_id"] != calls[1]["client_op_id"] == calls[2]["client_op_id"]
    assert "model_pin_mandatory" not in calls[1]["fields"] and "model_pin_mandatory" not in calls[2]["fields"]
    assert all(bool(op.get("import")) == importing for op in calls)
    assert tid in svc.board("default")["rows"]["tasks"]


def test_a_second_forbidden_column_is_final_not_an_unbounded_downgrade(live):
    kb.create_task(live, title="two unsupported fields", assignee="worker")
    live.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='tasks'")
    live.commit()
    calls = []
    def ops(board, batch):
        results = []
        for op in batch:
            if op["table"] == "tasks":
                calls.append(copy.deepcopy(op))
                column = "model_pin_mandatory" if "model_pin_mandatory" in op["fields"] else "priority"
                results.append({"client_op_id": op["client_op_id"], "status": "rejected",
                                "reason": "forbidden_field", "detail": f"not writable on tasks: {column}"})
            else:
                results.append({"client_op_id": op["client_op_id"], "status": "applied"})
        return {"results": results}
    hub = SimpleNamespace(ops=ops)
    for _ in range(4):
        client.push(live, hub, "default", "macbook", primary=True, max_batches=1)
    assert len(calls) == 2
    assert calls[0]["client_op_id"] != calls[1]["client_op_id"]
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name='tasks' AND state='rejected'").fetchone()[0] == 2
