"""Independent recovery fences must reject even when adjacent guards accept."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement_recovery as recovery
from hermes_cli import kanban_worker_node as node
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures

conn = fixtures.conn
setup = fixtures.setup


@pytest.mark.parametrize("change", ["delete", "version", "status"])
def test_snapshot_fence_without_pending_outbox(setup, change):
    db, _ = setup
    tid = fixtures.make(db)
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert recovery.unchanged_ready(db, row)
    if change == "delete":
        db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    elif change == "version":
        db.execute("UPDATE tasks SET sync_version=sync_version+1 WHERE id=?", (tid,))
    else:
        db.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    # Model a preceding authoritative feed import, not a locally pending write.
    db.execute("DELETE FROM kanban_sync_outbox WHERE row_uid=?", (tid,))
    db.commit()
    assert not recovery.unchanged_ready(db, row)


@pytest.mark.parametrize("machines", [{"worker-1": {"state": "revoked"}}, {}])
def test_pending_owner_move_preempts_stale_recovery(setup, monkeypatch, machines):
    db, _ = setup
    tid = fixtures.fixtures.acknowledged(db, placement_policy=wp.POLICY, runs_on='["machine:worker-1"]')
    kb._append_event(db, tid, "moved", {"runs_on": ["machine:worker-2"]})
    db.commit()
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": machines})
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert wp.requested_move(db, tid) == ["machine:worker-2"]
    assert recovery.recovery_mode(db, row, {}) is None


def test_promotion_helper_preserves_explicit_empty_labels(setup):
    db, _ = setup
    tid = fixtures.make(db, runs_on="[]")
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert recovery.promotion_mode(db, row, {}) is None


@pytest.mark.parametrize("payload", [None, "null", "[]", "broken"])
def test_missing_or_malformed_creation_never_promotes(setup, payload):
    db, _ = setup
    tid = fixtures.make(db)
    if payload is None:
        db.execute("DELETE FROM task_events WHERE task_id=? AND kind='created'", (tid,))
    else:
        db.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'", (payload, tid))
    db.commit()
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert recovery.promotion_mode(db, row, {}) is None
    assert not recovery.current_portable(db, row, {})


@pytest.mark.parametrize("kind", ["moved", "placement_repinned"])
def test_historical_owner_move_prevents_reenrollment(setup, kind):
    db, hub = setup
    tid = fixtures.make(db)
    kb._append_event(db, tid, kind, {"runs_on": []})
    db.commit()
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls


@pytest.mark.parametrize("value", [[1], {"terminal": True}, 5])
def test_current_requirement_shape_rejected_independently(setup, value):
    db, _ = setup
    tid = fixtures.make(db)
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    row['requires'] = json.dumps(value)
    assert not recovery.current_portable(db, row, {})


@pytest.mark.parametrize("policy", [None, wp.POLICY])
def test_stale_policy_without_pending_move_can_recover(setup, monkeypatch, policy):
    db, _ = setup
    tid = fixtures.make(db, runs_on='["machine:worker-1"]')
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    row["placement_policy"] = policy
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {"worker-1": {"hold": True}}})
    monkeypatch.setattr(wp, "requested_move", lambda *a: None if policy else ["machine:worker-2"])
    assert recovery.recovery_mode(db, row, {}) == "stale_pin"


def test_prior_policy_without_move_event_does_not_reenroll(setup):
    db, hub = setup
    tid = fixtures.make(db)
    db.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
               (json.dumps({"placement_policy": wp.POLICY}), tid))
    db.commit()
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls


@pytest.mark.parametrize("source", ["current", "created"])
def test_project_constraint_survives_reconstruction(setup, source):
    db, _ = setup
    tid = fixtures.make(db)
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    if source == "current":
        row["project_id"] = "project-custody"
    else:
        db.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                   (json.dumps({"project_id": "project-custody"}), tid))
        db.commit()
    assert not recovery.current_portable(db, row, {})


@pytest.mark.parametrize("key", ["requires", "skills"])
def test_bad_member_rejected_before_downstream_classifier(setup, monkeypatch, key):
    from hermes_cli import kanban_placement as placement
    db, _ = setup
    tid = fixtures.make(db)
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    row[key] = "[1]"
    monkeypatch.setattr(placement, "portable_reason", lambda *a: None)
    assert not recovery.current_portable(db, row, {})

