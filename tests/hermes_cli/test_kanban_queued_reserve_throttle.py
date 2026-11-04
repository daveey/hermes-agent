"""Queued placement is a bounded keepalive, not a dispatcher-frequency write."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_worker_placement as wp
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_worker_reserve_client as fixtures

conn = fixtures.conn
CFG = {"worker_pools": {"hub": ["worker-1"]}}


@pytest.mark.parametrize("entry", ["reevaluate", "dispatch_tick"])
def test_queued_throttle_revision_and_expiry(conn, monkeypatch, entry):
    tid = fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    clock = [100.0]
    monkeypatch.setattr(wp, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="hub"))
    monkeypatch.setattr(client, "Hub", lambda *a, **kw: hub)
    monkeypatch.setattr(placement, "config", lambda: CFG)
    def tick():
        if entry == "dispatch_tick":
            wp.dispatch_tick(conn)
        else:
            wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    tick()
    for _ in range(5):
        tick()
    assert len(hub.calls) == 1
    clock[0] += wp.RESERVE_KEEPALIVE_S
    tick()
    assert len(hub.calls) == 2
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET sync_version=5 WHERE id=?", (tid,))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.commit()
    tick()
    assert len(hub.calls) == 3


@pytest.mark.parametrize("state", ["queued", "assigned"])
def test_response_revision_and_assigned_renewal(conn, monkeypatch, state):
    tid = fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    original = hub.reserve
    def reserve(*args, **kwargs):
        return {**original(*args, **kwargs), "state": state, "task_version": 5}
    hub.reserve = reserve
    def pull(*args, **kwargs):
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET sync_version=5 WHERE id=?", (tid,))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
        conn.commit()
    monkeypatch.setattr(client, "pull", pull)
    for _ in range(3):
        wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert len(hub.calls) == (1 if state == "queued" else 3)


def test_explicit_move_bypasses_queued_cache(conn, monkeypatch):
    fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    calls = []
    hub.repin = lambda *a, **kw: calls.append(kw) or {"version": 5}
    monkeypatch.setattr(wp, "requested_move", lambda *a: ["machine:hub"])
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert len(calls) == 1
    assert len(hub.calls) == 1


def test_throttle_preserves_failure_stage_provenance(conn, monkeypatch):
    """A throttled pass records no error; the repin bypass and the post-expiry reserve keep their own stage."""
    from hermes_cli import kanban_queue_reason as qr

    tid = fixtures.acknowledged(conn)
    hub = fixtures.RecordingHub()
    clock = [100.0]
    monkeypatch.setattr(wp, "_queued", {})
    monkeypatch.setattr(wp, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)
    assert ("default", tid) in wp._queued
    queued = qr.latest(conn, tid)

    def refused(board, **body):
        raise ConnectionRefusedError(111, "x")
    hub.reserve = refused
    wp.reevaluate(conn, hub, "default", "hub", cfg=CFG)  # throttled: no Board request, so no reserve error
    assert qr.latest(conn, tid) == queued

    hub.repin = refused
    monkeypatch.setattr(wp, "requested_move", lambda *a: ["machine:hub"])
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 0  # explicit move bypasses the cache
    value = qr.latest(conn, tid)
    assert value["stage"] == "repin" and value["transport"] == "refused"

    monkeypatch.setattr(wp, "requested_move", lambda *a: None)
    clock[0] += wp.RESERVE_KEEPALIVE_S
    monkeypatch.setattr(qr, "record_error", lambda *a, **kw: calls.append(kw) or True)
    calls = []
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=CFG) == 0  # expired: re-asks and fails as a reserve
    assert [kw["stage"] for kw in calls] == ["reserve"]


def test_cache_has_hard_bound(conn, monkeypatch):
    fixtures.acknowledged(conn)
    monkeypatch.setattr(wp, "_queued", {("default", str(i)): (4, "", float("inf")) for i in range(1024)})
    monkeypatch.setattr(client, "pull", lambda *a, **kw: None)
    wp.reevaluate(conn, fixtures.RecordingHub(), "default", "hub", cfg=CFG)
    assert len(wp._queued) == 1024
