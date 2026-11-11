"""Older Boards do not support recovery; retain normal reservation service."""
import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures

conn = fixtures.conn
setup = fixtures.setup


@pytest.mark.parametrize("status", [404, 405, 501])
def test_absent_recovery_endpoint_backs_off_across_cards(setup, monkeypatch, status):
    db, hub = setup
    fixtures.make(db)
    fixtures.make(db)
    clock = [100.0]
    monkeypatch.setattr(wp.time, "monotonic", lambda: clock[0])
    calls = []

    def unsupported(board, **body):
        calls.append(body)
        raise client.HubError(status, "not found")

    hub.recover = unsupported
    wp.reevaluate(db, hub, "default", "hub")
    wp.reevaluate(db, hub, "default", "hub")
    assert len(calls) == 1
    clock[0] += 601
    wp.reevaluate(db, hub, "default", "hub")
    assert len(calls) == 2


def test_unavailable_recovery_keeps_policy_reservations(setup, monkeypatch):
    from hermes_cli import kanban_worker_node as node
    from tests.hermes_cli import test_kanban_worker_reserve_client as reserve
    db, hub = setup
    tid = reserve.acknowledged(db, runs_on='["machine:worker-1"]')
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {"worker-1": {"state": "revoked"}}})
    calls = []

    def unsupported(board, **body):
        raise client.HubError(404, "not found")

    hub.recover = unsupported
    hub.reserve = lambda board, **body: calls.append(body) or {"state": "assigned", "machine": "worker-2"}
    wp.reevaluate(db, hub, "default", "hub")
    wp.reevaluate(db, hub, "default", "hub")
    assert calls == [{"task_id": tid, "expected_revision": 4, "candidates": ["worker-2"]}]


def test_other_recovery_errors_remain_observable(setup):
    db, hub = setup
    fixtures.make(db)
    calls = []

    def conflict(board, **body):
        calls.append(body)
        raise client.HubError(409, '{"error":"revision_conflict"}')

    hub.recover = conflict
    wp.reevaluate(db, hub, "default", "hub")
    wp.reevaluate(db, hub, "default", "hub")
    assert len(calls) == 2
