"""Stale pins are recovery hints, never permission to assign without the Board."""
import json

import pytest

from hermes_cli import kanban_worker_node as node
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures

conn = fixtures.conn
setup = fixtures.setup


@pytest.mark.parametrize("entry", [
    {"state": "revoked"}, {"state": "active", "hold": True},
    {"state": "active", "last_seen": 1},
])
def test_stale_pin_recovery_requests_board_recheck(setup, monkeypatch, entry):
    conn, hub = setup
    tid = fixtures.make(conn, runs_on='["machine:worker-1", "linux"]')
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {"worker-1": entry}})
    assert wp.reevaluate(conn, hub, "default", "hub") == 1
    assert hub.calls[0][1] == {"task_id": tid, "expected_revision": 4,
                                "mode": "stale_pin", "candidates": ["worker-2"]}
    assert json.loads(conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0]) == [
        "machine:worker-1", "linux"]


@pytest.mark.parametrize("registry", [None, {}, {"machines": None},
    {"machines": {"worker-1": None}},
    {"machines": {"worker-1": {"state": "active"}}},
    {"machines": {"worker-1": {"state": "active", "last_seen": "bad"}}},
    {"machines": {"worker-1": {"state": "active", "last_seen": 0}}},
])
def test_unknown_registry_does_not_authorize_stale_recovery(setup, monkeypatch, registry):
    conn, hub = setup
    fixtures.make(conn, runs_on='["machine:worker-1"]')
    monkeypatch.setattr(node, "registry", lambda **kw: registry)
    assert wp.reevaluate(conn, hub, "default", "hub") == 0
    assert not hub.calls


@pytest.mark.parametrize("fields", [
    {"requires": '["host_repo"]'}, {"workspace_kind": "worktree"},
    {"lease_holder": "worker-1", "lease_expires": 1}, {"claim_lock": "owner"},
    {"status": "running"}, {"status": "review"}, {"created_on": "other"},
    {"body": "Read credentials on the hub"},
])
def test_stale_machine_never_overrides_independent_constraints(setup, monkeypatch, fields):
    conn, hub = setup
    fixtures.make(conn, runs_on='["machine:worker-1"]', **fields)
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {"worker-1": {"state": "revoked"}}})
    assert wp.reevaluate(conn, hub, "default", "hub") == 0
    assert not hub.calls


@pytest.mark.parametrize("age,expected", [(1800, 0), (1801, 1), (1799, 0), (-1, 0)])
def test_stale_threshold_uses_milliseconds(setup, monkeypatch, age, expected):
    from hermes_cli import kanban_placement_recovery as recovery
    conn, hub = setup
    fixtures.make(conn, runs_on='["machine:worker-1"]')
    monkeypatch.setattr(recovery.time, "time", lambda: 2000)
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {
        "worker-1": {"state": "active", "last_seen": (2000 - age) * 1000}}})
    assert wp.reevaluate(conn, hub, "default", "hub") == expected
