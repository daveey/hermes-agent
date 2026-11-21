"""Explicit rejections may advance identity; uncertain sends may not. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_envelope as fixtures

board = fixtures.board


class ParentRejected:
    status_origin_capability = fixtures.LostReply.status_origin_capability

    def __init__(self):
        self.batches = []

    def ops(self, board, wire):
        self.batches.append(wire)
        return {"results": [{"client_op_id": op["client_op_id"], "status": "rejected",
                             "reason": "storage_error"} for op in wire]}


def test_rejected_synthetic_parent_enters_bounded_retry_path(board):
    conn, _ = board
    tid = kb.create_task(conn, title="parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child")
    hub = ParentRejected()
    result = client.push(conn, hub, "default", "sender", primary=False)
    assert result.deferred >= 2
    original = next(op for op in hub.batches[0] if op["table"] == "tasks")
    saved = conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()
    assert saved and saved["op_id"] != original["client_op_id"]
    assert saved["attempts"] == 1
    assert saved["wire_json"] is None
    assert not conn.execute("SELECT 1 FROM kanban_sync_parent_envelopes").fetchone()


@pytest.mark.parametrize("reason", ["live_claim", "invalid", "storage_error"])
def test_synthetic_parent_rejection_does_not_accumulate_across_rounds(board, monkeypatch, reason):
    from types import SimpleNamespace
    conn, _ = board
    tid = kb.create_task(conn, title="parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child")
    batches = []

    def ops(_board, wire):
        batches.append(wire)
        return {"results": [{"client_op_id": op["client_op_id"], "status": "rejected",
                             "reason": reason if op["table"] == "tasks" else "depends_on_rejected"}
                            for op in wire]}

    now = [client.time.time()]
    monkeypatch.setattr(client.time, "time", lambda: now[0])
    for _ in range(8):
        client.push(conn, SimpleNamespace(ops=ops), "default", "sender", primary=False, max_batches=1)
        now[0] += 400
    rows = conn.execute("SELECT state, attempts FROM kanban_sync_outbox WHERE table_name='tasks'").fetchall()
    if reason == "storage_error":
        assert len(rows) == 1 and rows[0]["state"] == "pending"
        assert rows[0]["attempts"] == 8
    else:
        assert not rows
        assert len({next(op["client_op_id"] for op in wire if op["table"] == "tasks") for wire in batches}) == 8
    assert len({len(wire) for wire in batches}) == 1
    assert not conn.execute("SELECT 1 FROM task_events WHERE kind='sync_rejected'").fetchone()


def test_envelopes_commit_before_io_and_ack_cleans_pending_storage(board):
    conn, home = board
    kb.create_task(conn, title="durable", assignee="worker")

    class Ack:
        status_origin_capability = fixtures.LostReply.status_origin_capability

        def ops(self, board, wire):
            independent = kbc.connect(home / "kanban.db")
            try:
                for op in wire:
                    saved = independent.execute("SELECT wire_json FROM kanban_sync_outbox WHERE op_id=?",
                                                (op["client_op_id"],)).fetchone()
                    assert saved and json.loads(saved[0]) == op
            finally:
                independent.close()
            return {"results": [{"client_op_id": op["client_op_id"], "status": "applied", "version": 1}
                                for op in wire]}

    assert client.push(conn, Ack(), "default", "sender", primary=False).applied
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()


def test_delete_and_run_retries_freeze_payload_across_reopen(board, monkeypatch):
    conn, home = board
    tid = kb.create_task(conn, title="lifecycle", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET sync_version=1 WHERE id=?", (tid,))
        conn.execute("DELETE FROM kanban_sync_outbox")
        for table, uid, op, fields in [("task_runs", "run-frozen", "upsert", {"task_id": tid, "status": "running"}),
                                        ("task_chat_links", "delete-frozen", "delete", {})]:
            conn.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) VALUES (?,?,?,?,?,?,?)",
                         (sync.uuid7(), table, uid, op, json.dumps(fields), "", 1))
    hub = fixtures.LostReply()
    fixtures.send(conn, hub)
    conn.close()
    reopened = kbc.connect(home / "kanban.db")
    try:
        monkeypatch.setattr(client, "_next_hlc", lambda *_: pytest.fail("lifecycle retry re-clocked"))
        fixtures.send(reopened, hub)
        assert hub.batches[0] == hub.batches[1]
        assert {op["op"] for op in json.loads(hub.batches[0])} == {"upsert", "delete"}
    finally:
        reopened.close()


def test_parent_ack_and_pruning_remove_finished_pending_envelopes(board):
    from hermes_cli import kanban_sync_compat as compat
    from hermes_cli import kanban_sync_envelope as envelope
    conn, _ = board
    tid = kb.create_task(conn, title="synthetic", assignee="worker")
    wire = {"client_op_id": sync.uuid7(), "fields": {"title": "synthetic"}}
    with kbc.write_txn(conn):
        assert envelope.parent(conn, tid, lambda: wire) == wire
        compat.rejected_parent(conn, [(wire, tid)], {"client_op_id": wire["client_op_id"], "status": "applied", "version": 3})
        assert not conn.execute("SELECT 1 FROM kanban_sync_parent_envelopes").fetchone()
        envelope.parent(conn, "t_missing", lambda: wire)
        envelope.prune_parents(conn)
        assert not conn.execute("SELECT 1 FROM kanban_sync_parent_envelopes").fetchone()
