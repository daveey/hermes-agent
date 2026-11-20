"""Review round 2: unrelated deletes keep flowing, the hold is owner-visible and has a real release path."""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from tests.hermes_cli import test_kanban_containment_replica as fixtures
from tests.hermes_cli import test_kanban_receipt_quarantine_safety as safety

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


class Hub:
    def __init__(self):
        self.wires = []

    def ops(self, board, wire):
        self.wires.extend(wire)
        return {"results": []}


def _link(conn, task):
    conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_session,kind,created_at) "
                 "VALUES (?,?,?,?,?,?)", (f"link-{task}", task, "gw", "sess", "owner", int(time.time())))
    conn.execute("DELETE FROM kanban_sync_outbox")
    conn.execute("DELETE FROM task_chat_links WHERE task_id=?", (task,))


def test_unrelated_delete_of_chat_link_is_pushed_while_a_quarantine_exists(synced_conn):
    conn = synced_conn
    safety.conflict(conn)
    other = kb.create_task(conn, title="Unrelated", assignee="default")
    _link(conn, other)
    assert conn.execute("SELECT op FROM kanban_sync_outbox").fetchall()[0][0] == "delete"
    hub = Hub()
    result = ksc.push(conn, hub, "fixture", "worker", primary=False, max_batches=1)
    assert result.sent == 1
    assert len(hub.wires) == 1 and "delete" in json.dumps(hub.wires[0])


def test_unidentifiable_table_stays_pending_instead_of_killing_push(synced_conn):
    conn = synced_conn
    safety.conflict(conn)
    row = {"table_name": "mystery", "row_uid": "x", "op": "upsert", "fields": "{}"}
    assert quarantine.sendable(conn, [row]) == []


def test_hold_refusal_names_receipt_and_a_release_path_not_resume_contained(synced_conn):
    conn = synced_conn
    target, child, change, _hub, _ = safety.conflict(conn)
    for task in (target, child):
        text = connections.refusal(conn, task)
        assert "resume-contained required" not in text
        assert "quarantine --release" in text
    assert change.uid in connections.refusal(conn, target)
    listed = quarantine.entries(conn)
    assert [e["receipt"] for e in listed] == [change.uid]
    assert target in listed[0]["held_tasks"]
    assert ksc.status(conn)["quarantined"] == 1


def test_release_lifts_only_that_receipt_and_keeps_the_append_only_cache(synced_conn):
    conn = synced_conn
    target, child, change, _hub, original = safety.conflict(conn)
    from hermes_cli import kanban_containment_replica as replica
    assert quarantine.release(conn, "no-such-receipt") == 0
    assert quarantine.release(conn, change.uid) == 1
    assert not quarantine.held(conn, target) and not quarantine.held(conn, child)
    assert quarantine.entries(conn) == []
    assert replica._cached(conn, change.uid) == original  # the first observation is never rewritten
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'containment_sync_%'")}
    assert len(names) == 6  # immutability triggers are back after a release


def test_restore_consumes_checkpoint_file(synced_conn, tmp_path):
    conn = synced_conn
    safety.conflict(conn)
    path = tmp_path / "replica.db"
    quarantine.save_rebuild(conn, path)
    assert path.with_suffix(".quarantine.json").exists()
    data = quarantine.load_rebuild(path)
    quarantine.restore(conn, data, path)
    assert not path.with_suffix(".quarantine.json").exists()


def test_unreadable_file_fails_closed(tmp_path):
    bad = tmp_path / "shadow.db"
    bad.write_bytes(b"not a database" * 100)
    assert quarantine.file_holds(bad) is True
