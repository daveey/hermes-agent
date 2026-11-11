"""Promotion recomputes current inputs, without manufacturing Board assignments."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn


class Hub:
    def __init__(self):
        self.calls = []

    def recover(self, board, **body):
        self.calls.append((board, body))
        return {"state": "assigned", "machine": "worker-2", "task_version": 5}


@pytest.fixture
def setup(conn, monkeypatch):
    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-2"]}})
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    return conn, Hub()


def make(conn, **fields):
    tid = fixtures.acknowledged(conn, placement_policy=None, **fields)
    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                 (json.dumps({"placement_policy": None, "portable_reason": None}), tid))
    conn.commit()
    return tid


def test_promotion_recomputes_stale_creation_reason(setup):
    conn, hub = setup
    tid = make(conn)
    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                 (json.dumps({"portable_reason": "assignee not allowlisted"}), tid))
    conn.commit()
    assert wp.reevaluate(conn, hub, "default", "hub") == 1
    assert hub.calls == [("default", {"task_id": tid, "expected_revision": 4,
                                     "mode": "promote", "candidates": ["worker-2"]})]
    row = conn.execute("SELECT placement_policy,runs_on FROM tasks WHERE id=?", (tid,)).fetchone()
    assert tuple(row) == (None, None)  # response is not authority; only a feed pull may change this


@pytest.mark.parametrize("fields", [
    {"status": "todo"}, {"status": "running"}, {"status": "review"},
    {"claim_lock": "owner"}, {"lease_holder": "worker-1", "lease_expires": 1},
    {"created_on": "other"}, {"sync_version": None}, {"runs_on": "[]"},
    {"requires": '["host_repo"]'}, {"workspace_kind": "dir"},
    {"body": "Read secrets on the hub"},
])
def test_promotion_retains_independent_fences(setup, fields):
    conn, hub = setup
    make(conn, **fields)
    assert wp.reevaluate(conn, hub, "default", "hub") == 0
    assert not hub.calls


def test_promotion_does_not_trust_creation_portability(setup):
    conn, hub = setup
    tid = make(conn, requires='["host_repo"]')
    assert wp.reevaluate(conn, hub, "default", "hub") == 0
    assert not hub.calls
    assert kb.get_task(conn, tid).requires == ["host_repo"]


def test_promotion_preserves_explicit_local_intent(setup):
    conn, hub = setup
    tid = make(conn)
    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                 (json.dumps({"placement_intent": "explicit"}), tid))
    conn.commit()
    assert wp.reevaluate(conn, hub, "default", "hub") == 0
    assert not hub.calls


def test_recovery_http_payload(monkeypatch):
    hub = client.Hub(client.Identity("http://localhost", "synthetic", "hub"))
    calls = []
    monkeypatch.setattr(hub, "_call", lambda *a, **kw: calls.append((a, kw)) or {})
    hub.recover("default", task_id="task", expected_revision=4, mode="promote", candidates=["worker-2"])
    assert calls == [(("POST", "/v2/boards/default/placement/recover", {
        "task_id": "task", "expected_revision": 4, "mode": "promote",
        "policy": wp.POLICY, "candidates": ["worker-2"]}), {})]
