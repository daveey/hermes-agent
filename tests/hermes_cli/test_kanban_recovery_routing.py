"""Recovery and renewal retain independent routing and diagnostic ownership."""
import json

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_queue_reason as queue
from hermes_cli import kanban_worker_node as node
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures
from tests.hermes_cli import test_kanban_worker_reserve_client as reserve

conn = fixtures.conn
setup = fixtures.setup


def test_legacy_rows_do_not_advance_policy_cursor(setup):
    db, hub = setup
    policy = [reserve.acknowledged(db) for _ in range(wp.MAX_RESERVES_PER_TICK + 1)]
    legacy = [fixtures.make(db) for _ in range(wp.MAX_RESERVES_PER_TICK)]
    assert min(legacy) > max(policy)
    calls = []
    hub.reserve = lambda board, **body: calls.append(body["task_id"]) or {"state": "assigned"}
    wp.reevaluate(db, hub, "default", "hub")
    assert sync.sync_state(db)["worker_reserve_cursor"] == sorted(policy)[wp.MAX_RESERVES_PER_TICK - 1]
    wp.reevaluate(db, hub, "default", "hub")
    assert set(policy) <= set(calls)


def test_later_enrolled_card_renews_without_repromotion(setup):
    db, hub = setup
    tid = fixtures.make(db)
    db.execute("UPDATE tasks SET placement_policy=? WHERE id=?", (wp.POLICY, tid))
    db.execute("DELETE FROM kanban_sync_outbox")
    db.commit()
    calls = []
    hub.reserve = lambda board, **body: calls.append(body["task_id"]) or {"state": "assigned"}
    wp.reevaluate(db, hub, "default", "hub")
    assert calls == [tid]
    assert not hub.calls


def test_stale_pin_recovery_supersedes_historical_move(setup, monkeypatch):
    db, hub = setup
    tid = fixtures.make(db, runs_on='["machine:worker-1"]')
    db.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
               (tid, "moved", json.dumps({"runs_on": ["machine:worker-1"]}), 1))
    db.commit()
    assert wp.requested_move(db, tid) == ["machine:worker-1"]
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {"worker-1": {"state": "revoked"}}})
    repins = []
    hub.repin = lambda *a, **kw: repins.append(kw) or {}
    wp.reevaluate(db, hub, "default", "hub")
    assert not repins
    assert hub.calls[0][1]["mode"] == "stale_pin"


def test_recovery_transport_failure_records_recover_stage(setup, monkeypatch):
    db, hub = setup
    fixtures.make(db)
    def fail(*args, **kwargs):
        raise OSError("fixture transport offline")
    hub.recover = fail
    stages = []
    monkeypatch.setattr(queue, "record_error", lambda *a, **kw: stages.append(kw["stage"]))
    wp.reevaluate(db, hub, "default", "hub")
    assert stages == ["recover"]
