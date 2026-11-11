"""Recovery cannot infer machine failure from an unavailable registry."""
import pytest

from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as node
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_placement_recovery as fixtures

conn = fixtures.conn
setup = fixtures.setup


def test_failed_registry_refresh_does_not_turn_cached_seen_into_stale(setup, monkeypatch):
    db, hub = setup
    fixtures.make(db, runs_on='["machine:worker-1"]')
    clock = [100.0]
    monkeypatch.setattr(node.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(node.time, "time", lambda: clock[0])

    def fetch():
        if 100 < clock[0] < 3000:
            raise OSError("registry offline")
        return {"machines": [{"name": "worker-1", "state": "active", "last_seen": 100000}]}

    node.set_registry_fetch(fetch)
    try:
        assert node.registry()["machines"]["worker-1"]["last_seen"] == 100000
        clock[0] = 2000
        assert wp.reevaluate(db, hub, "default", "hub") == 0
        assert not hub.calls
        # Ordinary discovery retains its historical last-good cache contract.
        assert node.registry()["machines"]["worker-1"]["last_seen"] == 100000
        assert wp.reevaluate(db, hub, "default", "hub") == 0  # retry TTL is not freshness
        clock[0] = 3000
        assert wp.reevaluate(db, hub, "default", "hub") == 1  # affirmative refresh restores recovery
    finally:
        node.set_registry_fetch(None)


def test_dispatch_without_pool_does_not_pull(setup, monkeypatch):
    db, _ = setup
    fixtures.make(db)
    monkeypatch.setattr(placement, "config", dict)
    monkeypatch.setattr(client, "load_identity", lambda: client.Identity("http://localhost", "synthetic", "hub"))
    calls = []
    monkeypatch.setattr(client, "pull", lambda *a, **kw: calls.append(True))
    wp.dispatch_tick(db)
    assert not calls


@pytest.mark.parametrize("labels", [None, '["machine:worker-1"]'])
def test_courier_custody_blocks_recovery(setup, monkeypatch, labels):
    db, hub = setup
    tid = fixtures.make(db, runs_on=labels)
    db.execute("CREATE TABLE origin_creation_intents (task_id TEXT)")
    db.execute("INSERT INTO origin_creation_intents VALUES (?)", (tid,))
    db.commit()
    monkeypatch.setattr(node, "registry", lambda **kw: {"machines": {"worker-1": {"state": "revoked"}}})
    assert wp.reevaluate(db, hub, "default", "hub") == 0
    assert not hub.calls
    db.execute("DELETE FROM origin_creation_intents WHERE task_id=?", (tid,))
    db.commit()
    assert wp.reevaluate(db, hub, "default", "hub") == 1  # an unrelated/empty ledger is not a pin
