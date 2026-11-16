"""Fresh active-list absence proposes Board adjudication, not local reassignment."""
import pytest

from hermes_cli import kanban_placement_recovery as recovery
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as node
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures

conn = fixtures.conn
setup = fixtures.setup


@pytest.fixture
def registry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(node.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(node, "_REGISTRY", dict(at=None, value=None, retry=False, ok_at=None))
    monkeypatch.setattr(node, "_FETCH", lambda: {"machines": []})
    return clock


def test_absent_pin_proposes_board_with_revision_and_retains_labels(setup, registry):
    db, hub = setup
    tid = fixtures.make(db, runs_on='["machine:retired", "linux"]')
    row = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert recovery.recovery_mode(db, row, {}) == "stale_pin"
    assert wp.reevaluate(db, hub, "default", "hub") == 1
    assert hub.calls == [("default", {"task_id": tid, "expected_revision": 4,
                                     "mode": "stale_pin", "candidates": ["worker-2"]})]
    assert dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == row


def test_unknown_name_board_refusal_is_unchanged_and_bounded(setup, registry):
    db, hub = setup
    tid = fixtures.make(db, runs_on='["machine:unknown", "linux"]')
    before = dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())

    def reject(board, **body):
        hub.calls.append((board, body))
        raise client.HubError(409, '{"error":"pin_not_stale"}')

    hub.recover = reject
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert len(hub.calls) == 1
    assert dict(db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert len(hub.calls) == 1
    registry[0] += 601
    wp.reevaluate(db, hub, "default", "hub")
    assert len(hub.calls) == 2


@pytest.mark.parametrize("raw", [None, {}, [], {"machines": None}, {"machines": {}},
                                     {"machines": [None]}, {"machines": [{}]},
                                     {"machines": [{"name": 3}]},
                                     {"machines": [{"name": "x"}, {"name": "x"}]}])
@pytest.mark.parametrize("had_good", [False, True])
def test_malformed_registry_never_becomes_fresh_absence(setup, registry, monkeypatch, raw, had_good):
    db, hub = setup
    fixtures.make(db, runs_on='["machine:retired"]')
    if had_good:
        assert node.registry(require_fresh=True) == {"me": None, "machines": {}}
        registry[0] += node.REGISTRY_TTL_S + 1
    monkeypatch.setattr(node, "_FETCH", lambda: raw)
    assert node.registry(require_fresh=True) is None
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls


@pytest.mark.parametrize("had_good", [False, True])
def test_outage_or_expired_last_good_cannot_propose_absence(setup, registry, monkeypatch, had_good):
    db, hub = setup
    fixtures.make(db, runs_on='["machine:retired"]')
    if had_good:
        assert node.registry(require_fresh=True) == {"me": None, "machines": {}}
        registry[0] += node.REGISTRY_TTL_S + 1

    def offline():
        raise OSError("offline")

    monkeypatch.setattr(node, "_FETCH", offline)
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls


@pytest.mark.parametrize("fields", [
    {"requires": '["host_repo"]'}, {"workspace_kind": "worktree"},
    {"workspace_path": "/unpublished"}, {"lease_holder": "retired", "lease_expires": 1},
    {"claim_lock": "owner"}, {"status": "running"}, {"status": "review"},
    {"created_on": "other"}, {"sync_version": None}, {"body": "Read credentials on the hub"},
    {"runs_on": '["machine:retired", "machine:other"]'},
])
def test_absence_retains_independent_fences(setup, registry, fields):
    db, hub = setup
    fixtures.make(db, **{"runs_on": '["machine:retired"]', **fields})
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls


def test_absence_does_not_recover_previously_run_card(setup, registry):
    db, hub = setup
    tid = fixtures.make(db, runs_on='["machine:retired"]')
    db.execute("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES (?, 'default', 'done', 1)", (tid,))
    db.commit()
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls
