"""Safety boundaries for task-scoped receipt quarantine (fixture Board only)."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def conflict(conn):
    target, child, stop, _, hub = fixtures.scenario(conn)
    proof = hub.events[stop["event_uid"]]
    replica.remember(conn, proof, board="fixture", task=target, uid=stop["event_uid"])
    original = copy.deepcopy(proof)
    proof["row"]["created_at"] += 1
    change = fixtures.event(stop, hub, 1)
    assert quarantine.certified_page(conn, hub, "fixture", [change]) == []
    return target, child, change, hub, original


def test_hold_blocks_claim_pickup_push_and_lifecycle_without_event(synced_conn):
    conn = synced_conn
    target, child, change, _hub, _ = conflict(conn)
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (change.uid,)).fetchone() is None
    for task in (target, child):
        assert connections.held(conn, task)
        assert kb.claim_task(conn, task) is None
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task,))
    with pytest.raises(ContainmentRefused, match="quarantined"):
        replica.adopt_receipts(conn, conn, [sync.RemoteChange(3, "tasks", target, "upsert", {}, 3, "", "worker")])
    row = {"table_name": "tasks", "row_uid": target, "fields": json.dumps({"title": "unsafe push"})}
    other = kb.create_task(conn, title="Independent", assignee="default")
    healthy = {**row, "row_uid": other}
    assert quarantine.sendable(conn, [row, healthy]) == [healthy]


def test_good_redelivery_never_erases_conflict_and_logs_once(synced_conn, caplog):
    conn = synced_conn
    target, _, change, hub, original = conflict(conn)
    quarantine.certified_page(conn, hub, "fixture", [change])
    assert sum("quarantined receipt" in r.message for r in caplog.records) == 1
    hub.events[change.uid] = original
    assert quarantine.certified_page(conn, hub, "fixture", [fixtures.event({"event_uid": change.uid}, hub, 2)]) == []
    assert connections.held(conn, target)
    with pytest.raises(sqlite3.IntegrityError, match="quarantine_immutable"):
        conn.execute("DELETE FROM containment_sync_quarantine")
    with pytest.raises(sqlite3.IntegrityError, match="quarantine_immutable"):
        conn.execute("UPDATE containment_sync_quarantine SET reason='cleared'")


def test_later_task_and_new_dependency_pages_are_withheld(synced_conn):
    conn = synced_conn
    target, _, _, hub, _ = conflict(conn)
    other = kb.create_task(conn, title="New dependent", assignee="default")
    task = sync.RemoteChange(2, "tasks", target, "upsert", {"status": "ready", "containment_episode": None}, 2, "", "worker")
    edge = sync.RemoteChange(3, "task_links", f"{target}>{other}", "upsert", {}, 3, "", "worker")
    title = sync.RemoteChange(4, "tasks", other, "upsert", {"title": "Must wait"}, 4, "", "worker")
    assert quarantine.certified_page(conn, hub, "fixture", [task, edge, title]) == []
    assert connections.held(conn, other)
    assert conn.execute("SELECT count(*) FROM containment_sync_withheld").fetchone()[0] == 4


def test_journal_failure_does_not_advance_cursor(synced_conn, monkeypatch):
    conn = synced_conn
    target, _, change, hub, _original = conflict(conn)
    old = sync.sync_state(conn)["cursor"]
    monkeypatch.setattr(quarantine, "_wire", lambda ch: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(OSError, match="disk unavailable"):
        quarantine.certified_page(conn, hub, "fixture", [change])
    assert sync.sync_state(conn)["cursor"] == old
    assert connections.held(conn, target)


def test_rebuild_checkpoint_survives_loss_of_old_database(synced_conn, tmp_path):
    conn = synced_conn
    target, _, change, _hub, original = conflict(conn)
    path = tmp_path / "replica.db"
    quarantine.save_rebuild(conn, path)
    # The checkpoint must already be readable when the caller is about to unlink
    # the old DB. A crash before the new DB restore must not lose the first receipt.
    data = quarantine.load_rebuild(path)
    assert data["containment_sync_quarantine"][0][0] == change.uid
    # Reuse fixture schema but remove only transient target material via a fresh
    # real board connection; no live stores are opened by this test.
    from hermes_cli import kanban_db_connect
    with kanban_db_connect.connect_closing(db_path=tmp_path / "rebuilt.db") as fresh:
        sync.set_identity(fresh, "fixture", "worker")
        quarantine.restore(fresh, data)
        assert quarantine.held(fresh, target)
        assert replica._cached(fresh, change.uid) == original
