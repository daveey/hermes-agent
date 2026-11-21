"""Receipt-aware refusal recovery and bounded parent custody. [daveey]"""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_envelope as fixtures

board = fixtures.board


@pytest.mark.parametrize("reason", ["live_claim", "invalid"])
def test_definitive_parent_refusal_retires_receipt_identity(board, monkeypatch, reason):
    conn, _ = board
    tid = kb.create_task(conn, title="parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child")
    receipts, known, parent_ids = {}, set(), []
    refusing = [True]

    def ops(_board, wire):
        results = []
        for op in wire:
            cid = op["client_op_id"]
            if cid not in receipts:
                if op["table"] == "tasks":
                    if refusing[0]:
                        receipts[cid] = {"status": "rejected", "reason": reason}
                    else:
                        known.add(op["uid"])
                        receipts[cid] = {"status": "applied", "version": 1}
                elif op.get("fields", {}).get("task_id") in known:
                    receipts[cid] = {"status": "applied", "version": 1}
                else:
                    receipts[cid] = {"status": "rejected", "reason": "depends_on_rejected"}
            if op["table"] == "tasks":
                parent_ids.append(cid)
            results.append({"client_op_id": cid, **receipts[cid]})
        return {"results": results}

    now = [client.time.time()]
    monkeypatch.setattr(client.time, "time", lambda: now[0])
    for i in range(10):
        refusing[0] = i < 3
        client.push(conn, SimpleNamespace(ops=ops), "default", "sender", primary=False, max_batches=1)
        now[0] += 400
        assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()
        assert not conn.execute("SELECT 1 FROM task_events WHERE kind='sync_rejected'").fetchone()
    assert tid in known
    assert len(parent_ids) == len(set(parent_ids)) == 4
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()
    assert not conn.execute("SELECT 1 FROM kanban_sync_parent_envelopes").fetchone()


def test_new_children_do_not_resynthesize_parent_during_backoff(board, monkeypatch):
    conn, _ = board
    tid = kb.create_task(conn, title="parent", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    kb.add_comment(conn, tid, "author", "child")
    down, delivered = [True], []

    def ops(_board, wire):
        results = []
        for op in wire:
            if down[0]:
                reason = "storage_error" if op["table"] == "tasks" else "depends_on_rejected"
                result = {"status": "rejected", "reason": reason}
            else:
                delivered.append(op)
                result = {"status": "applied", "version": len(delivered)}
            results.append({"client_op_id": op["client_op_id"], **result})
        return {"results": results}

    hub = SimpleNamespace(ops=ops)
    now = [client.time.time()]
    monkeypatch.setattr(client.time, "time", lambda: now[0])
    for i in range(6):
        client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
        now[0] += 400
        for j in range(2):
            kb.add_comment(conn, tid, "author", f"later child {i}/{j}")
            client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
            rows = conn.execute("SELECT state FROM kanban_sync_outbox WHERE table_name='tasks'").fetchall()
            assert len(rows) == 1 and rows[0][0] == "pending"
    down[0] = False
    for _ in range(4):
        now[0] += 400
        client.push(conn, hub, "default", "sender", primary=False, max_batches=1)
    assert len([op for op in delivered if op["table"] == "tasks"]) == 1
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()
