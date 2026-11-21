"""Capture provenance survives definitive compatibility repairs, never uncertain rewrites."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_compat as compat
from hermes_cli import kanban_sync_envelope as envelopes
from tests.hermes_cli import test_kanban_sync_envelope as fixtures

board = fixtures.board


@pytest.mark.parametrize("repair", ["parent", "virtual", "omission"])
def test_definitive_repair_retains_captured_origin_and_freezes_next_send(board, repair):
    conn, _ = board
    tid = kb.create_task(conn, title="repair", assignee="worker")
    original = dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (tid,)).fetchone())
    assert original["status_origin"] == ""
    wire = {"client_op_id": original["op_id"], "op": "upsert", "table": "tasks", "uid": tid,
            "fields": json.loads(original["fields"]), "hlc": "1790000000000-0000-test", "status_origin": ""}
    with kbc.write_txn(conn):
        if repair == "parent":
            conn.execute("DELETE FROM kanban_sync_outbox")
            result = {"client_op_id": wire["client_op_id"], "status": "rejected", "reason": "storage_error"}
            retried = compat.rejected_parent(conn, [(wire, tid)], result)
        else:
            if repair == "virtual":
                wire["fields"]["branch_name"] = "not-deliverable"
                original["fields"] = json.dumps(wire["fields"])
                result = {"reason": "forbidden_field", "detail": "not writable on tasks: branch_name"}
            else:
                result = {"reason": "forbidden_field", "detail": "not writable on tasks: assignee"}
            assert compat.enqueue_retry(conn, original, wire, result)
            retried = dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE row_uid=? ORDER BY rowid DESC LIMIT 1", (tid,)).fetchone())
    assert retried["status_origin"] == ""
    def build():
        return {**wire, "client_op_id": retried["op_id"], "fields": json.loads(retried["fields"])}
    with kbc.write_txn(conn):
        first = envelopes.outbound(conn, retried, build, supported=True)
    assert first["status_origin"] == ""
    def no_rebuild():
        pytest.fail("uncertain repaired envelope was rebuilt")
    with kbc.write_txn(conn):
        assert envelopes.outbound(conn, retried, no_rebuild, supported=True) == first


def test_trigger_detach_and_reattach_invalidates_synthetic_capture_descriptor(board):
    conn, _ = board
    tid = kb.create_task(conn, title="detach", assignee="worker")
    assert conn.execute("SELECT origin FROM kanban_sync_status_origins WHERE task_id=?", (tid,)).fetchone()[0] == ""
    with kbc.write_txn(conn):
        sync.drop_triggers(conn)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completed", {})
        sync.install_triggers(conn)
    assert not conn.execute("SELECT 1 FROM kanban_sync_status_origins WHERE task_id=?", (tid,)).fetchone()


def test_unseen_full_snapshot_keeps_current_status_and_its_own_capture_origin(board):
    conn, _ = board
    tid = kb.create_task(conn, title="offline-completed", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completed", {})
    completed = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()[0]
    class Guarded(fixtures.LostReply):
        def status_origin_capability(self, board):
            return fixtures.GUARD_CAP
    hub = Guarded()
    fixtures.send(conn, hub)
    task_wires = [op for op in json.loads(hub.batches[0]) if op.get("table") == "tasks"]
    assert task_wires
    # Full snapshots carry current metadata; old intermediate status must not be
    # spliced into that row or publish a newly executable ready task.
    assert all(op["fields"]["status"] == "done" for op in task_wires)
    assert all(op["status_origin"] == completed for op in task_wires)

