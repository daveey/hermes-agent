"""A later home generation never upgrades already captured/serialized intent."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_envelope as envelope
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)


def generation(conn, task, nonce):
    with kbc.write_txn(conn):
        # Disposable source mirror of a future authenticated committed handoff.
        # No production migration or activation API exists in this slice.
        conn.execute("CREATE TABLE IF NOT EXISTS kanban_home_generations1(task_id TEXT PRIMARY KEY, nonce TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO kanban_home_generations1 VALUES (?,?)", (task, nonce))


def captured(conn, table, uid=None):
    row = conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name=? "
                       + ("AND row_uid=? " if uid is not None else "") + "ORDER BY rowid DESC LIMIT 1",
                       (table, uid) if uid is not None else (table,)).fetchone()
    return dict(row)


def wire(conn, row):
    return envelope.outbound(conn, row, lambda: {"client_op_id": row["op_id"], "op": row["op"],
        "table": row["table_name"], "uid": row["row_uid"], "fields": json.loads(row["fields"]), "hlc": "100-0-test"})


def test_late_handoff_does_not_certify_old_intent_but_new_native_capture_carries_generation(board):
    task = kb.create_task(board, title="source before handoff")
    kb.add_comment(board, task, "actor", "old capture")
    old = captured(board, "task_comments")
    generation(board, task, "committed-nonce-0001")
    with kbc.write_txn(board):
        old_wire = wire(board, old)
    assert "home_generations" not in old_wire
    kb.add_comment(board, task, "actor", "new capture")
    new = captured(board, "task_comments")
    with kbc.write_txn(board):
        new_wire = wire(board, new)
    assert new_wire.get("home_generations") == {task: "committed-nonce-0001"}
    assert "home_generations" not in old_wire


def test_resend_preserves_captured_generation_not_current_mirror(board):
    task = kb.create_task(board, title="first generation")
    generation(board, task, "committed-nonce-0001")
    kb.add_comment(board, task, "actor", "pending intent")
    row = captured(board, "task_comments")
    generation(board, task, "committed-nonce-0002")
    with kbc.write_txn(board):
        first = wire(board, row)
    assert first.get("home_generations") == {task: "committed-nonce-0001"}
    with kbc.write_txn(board):
        assert wire(board, row) == first


def test_manual_reconstruction_never_gains_current_generation(board):
    task = kb.create_task(board, title="source")
    generation(board, task, "committed-nonce-0001")
    with kbc.write_txn(board):
        board.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                      "VALUES('reconstructed','tasks',?,'upsert','{\"title\":\"historical\"}','',1)", (task,))
        row = captured(board, "tasks", task)
        assert "home_generations" not in wire(board, row)


@pytest.mark.parametrize("table, columns, values", [
    ("tasks", "id,title,status,created_at", "?,'after handoff','todo',1"),
    ("task_comments", "task_id,author,body,created_at", "?,'actor','after handoff',1"),
    ("task_events", "task_id,kind,payload,created_at", "?,'diagnostic','{}',1"),
    ("task_runs", "task_id,profile,status,started_at", "?,'default','done',1"),
    ("task_attachments", "task_id,filename,stored_path,size,created_at", "?,'proof.txt','fixture-only',0,1"),
    ("task_chat_links", "task_id,chat_gateway,chat_profile,chat_session,kind,created_at", "?,'hub','default','fixture','shepherd',1"),
])
def test_each_native_insert_captures_at_the_writer_transaction(board, table, columns, values):
    task = kb.create_task(board, title="before handoff")
    generation(board, task, "committed-nonce-0001")
    if table == "tasks":
        # Exercise native tasks insertion without a conflicting source row.
        with kbc.write_txn(board):
            board.execute("UPDATE kanban_sync_state SET applying=1")
            board.execute("DELETE FROM tasks WHERE id=?", (task,))
            board.execute("UPDATE kanban_sync_state SET applying=0")
    with kbc.write_txn(board):
        board.execute(f"INSERT INTO {table}({columns}) VALUES ({values})", (task,))
    row = captured(board, table)
    assert json.loads(row["home_generations"]) == {task: "committed-nonce-0001"}
    with kbc.write_txn(board):
        assert wire(board, row)["home_generations"] == {task: "committed-nonce-0001"}


def test_native_task_update_and_delete_keep_epoch_after_source_row_disappears(board):
    task = kb.create_task(board, title="before handoff")
    generation(board, task, "committed-nonce-0001")
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET title='new intent' WHERE id=?", (task,))
    update = captured(board, "tasks", task)
    assert json.loads(update["home_generations"]) == {task: "committed-nonce-0001"}
    with kbc.write_txn(board):
        board.execute("DELETE FROM tasks WHERE id=?", (task,))
    delete = captured(board, "tasks", task)
    assert delete["op"] == "delete"
    with kbc.write_txn(board):
        assert wire(board, delete)["home_generations"] == {task: "committed-nonce-0001"}


def test_pending_generation_cannot_be_rewritten_to_the_current_epoch(board):
    import sqlite3
    task = kb.create_task(board, title="source")
    row = captured(board, "tasks", task)
    generation(board, task, "committed-nonce-0001")
    with pytest.raises(sqlite3.IntegrityError, match="captured home generation is immutable"):
        with kbc.write_txn(board):
            board.execute("UPDATE kanban_sync_outbox SET home_generations=? WHERE op_id=?",
                          (json.dumps({task: "committed-nonce-0001"}), row["op_id"]))
    assert captured(board, "tasks", task)["home_generations"] is None


def test_link_capture_binds_both_endpoints_and_delete_keeps_the_generation(board):
    parent = kb.create_task(board, title="parent")
    child = kb.create_task(board, title="child")
    generation(board, parent, "committed-parent-001")
    generation(board, child, "committed-child-0001")
    with kbc.write_txn(board):
        board.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (parent, child))
    linked = captured(board, "task_links")
    with kbc.write_txn(board):
        assert wire(board, linked).get("home_generations") == {parent: "committed-parent-001", child: "committed-child-0001"}
        board.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (parent, child))
    deleted = captured(board, "task_links")
    assert deleted["op"] == "delete"
    with kbc.write_txn(board):
        assert wire(board, deleted).get("home_generations") == {parent: "committed-parent-001", child: "committed-child-0001"}
