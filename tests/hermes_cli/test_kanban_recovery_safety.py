"""Recovery must not move unpublished work or locally diverged specifications."""
import json

import pytest

from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures

conn = fixtures.conn
setup = fixtures.setup


@pytest.mark.parametrize("fields", [
    {"workspace_path": "/retained/scratch"},
    {"requires": "5"}, {"skills": "5"},
    {"requires": '{"terminal": true}'},
])
def test_recovery_rejects_unsafe_current_inputs(setup, fields):
    db, hub = setup
    fixtures.make(db, **fields)
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert hub.calls == []


def test_recovery_rejects_rejected_local_spec(setup):
    db, hub = setup
    tid = fixtures.make(db)
    db.execute("UPDATE tasks SET body='changed specification' WHERE id=?", (tid,))
    db.execute("UPDATE kanban_sync_outbox SET state='rejected' WHERE table_name='tasks' AND row_uid=?", (tid,))
    db.commit()
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert hub.calls == []


def test_recovery_rechecks_local_spec_between_calls(setup):
    db, hub = setup
    first, second = fixtures.make(db), fixtures.make(db)
    original = hub.recover

    def recover(board, **body):
        other = second if body['task_id'] == first else first
        db.execute("UPDATE tasks SET requires=? WHERE id=?", ('["host_repo"]', other))
        db.commit()
        return original(board, **body)

    hub.recover = recover
    assert wp.reevaluate(db, hub, "default", "hub") == 1
    assert len(hub.calls) == 1


def test_audit_failure_retains_explicit_intent(monkeypatch):
    from hermes_cli import kanban_placement as placement

    def unavailable(*args):
        raise OSError("unavailable")

    monkeypatch.setattr(placement, "portable_reason", unavailable)
    result = wp.creation_audit({"placement": "local"}, {})
    assert result == {"portable_reason": "audit failed", "placement_intent": "explicit"}


@pytest.mark.parametrize("payload", ["null", "[]", "5", "broken"])
def test_invalid_creation_provenance_is_not_portable(setup, payload):
    db, hub = setup
    tid = fixtures.make(db)
    db.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'", (payload, tid))
    db.commit()
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls


@pytest.mark.parametrize("labels", [None, '["machine:worker-1"]'])
def test_prior_run_preserves_unpublished_custody(setup, monkeypatch, labels):
    from hermes_cli import kanban_worker_node as node
    db, hub = setup
    tid = fixtures.make(db, runs_on=labels)
    db.execute("INSERT INTO task_runs (task_id,profile,status,started_at) VALUES (?,'default','completed',1)", (tid,))
    db.commit()
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {"worker-1": {"state": "revoked"}}})
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls


def test_stored_creation_path_is_retained(setup):
    db, hub = setup
    tid = fixtures.make(db)
    db.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
               (json.dumps({"workspace_path": "/declared/input"}), tid))
    db.commit()
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls
