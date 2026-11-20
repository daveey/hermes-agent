"""Advisory review reproductions, turned into desired-behavior regressions."""
from __future__ import annotations

import copy

import pytest

from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from tests.hermes_cli import test_kanban_containment_replica as fixtures
from tests.hermes_cli import test_kanban_receipt_quarantine_safety as safety

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def test_held_fifo_prefix_does_not_starve_healthy_push(synced_conn):
    conn = synced_conn
    target, _, _, _, _ = safety.conflict(conn)
    conn.execute("DELETE FROM kanban_sync_outbox")
    for n in range(ksc.PUSH_BATCH):
        conn.execute("UPDATE tasks SET title=? WHERE id=?", (f"held-{n}", target))
    healthy = kb.create_task(conn, title="Healthy later", assignee="default")

    class Hub:
        def __init__(self):
            self.wires = []

        def ops(self, board, wire):
            self.wires.extend(wire)
            return {"results": []}

    hub = Hub()
    result = ksc.push(conn, hub, "fixture", "worker", primary=False, max_batches=1)
    assert result.sent > 0
    assert any(op["uid"] == healthy for op in hub.wires)
    assert all(op["uid"] != target for op in hub.wires)


def test_hold_adoption_independent_of_destination_receipt_conflict(synced_conn, tmp_path):
    source = synced_conn
    target, _, stop, _, hub = fixtures.scenario(source)
    original = copy.deepcopy(hub.events[stop["event_uid"]])
    hub.events[stop["event_uid"]]["row"]["created_at"] += 1
    replica.certify_page(source, hub, "fixture", [fixtures.event(stop, hub, 1)])
    hub.events[stop["event_uid"]]["row"]["created_at"] += 1
    quarantine.certified_page(source, hub, "fixture", [fixtures.event(stop, hub, 2)])
    with kbc.connect_closing(db_path=tmp_path / "live.db") as live:
        sync.set_identity(live, "fixture", "worker")
        replica.remember(live, original, board="fixture", task=target, uid=stop["event_uid"])
        quarantine.adopt(source, live)
        assert quarantine.held(live, target)
        assert replica._cached(live, stop["event_uid"]) == original


def test_quarantine_mirrored_before_later_page_transport_failure(synced_conn, tmp_path, monkeypatch):
    source = synced_conn
    target, _, stop, _, hub = fixtures.scenario(source)
    replica.remember(source, hub.events[stop["event_uid"]], board="fixture", task=target, uid=stop["event_uid"])
    hub.events[stop["event_uid"]]["row"]["created_at"] += 1
    change = fixtures.event(stop, hub, 1)
    count = 0

    def page(*args):
        nonlocal count
        count += 1
        if count == 1:
            return {"changes": [change], "next": 1, "more": True}
        raise OSError("transport unavailable after quarantine")

    monkeypatch.setattr(ksc.acw, "changes_page", page)
    monkeypatch.setattr(ksc, "adapt", lambda change: change)
    with kbc.connect_closing(db_path=tmp_path / "live.db") as live:
        sync.set_identity(live, "fixture", "worker")
        with pytest.raises(OSError, match="transport unavailable"):
            with quarantine.mirroring(live):
                ksc.pull(source, hub, "fixture", observers=False)
        assert quarantine.held(source, target)
        assert quarantine.held(live, target)
