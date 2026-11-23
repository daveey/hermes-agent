"""Real capture transactions associate status with their own lifecycle evidence."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_sync_envelope as fixtures

board = fixtures.board


def status_ops(conn, tid):
    return [dict(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? ORDER BY rowid", (tid,))
            if "status" in json.loads(r["fields"])]


def fresh(conn):
    tid = kb.create_task(conn, title="capture", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        conn.execute("DELETE FROM kanban_sync_outbox")
    return tid


@pytest.mark.parametrize("event_first", [False, True])
def test_transaction_binds_its_own_event_in_both_orders(board, event_first):
    conn, _ = board
    tid = fresh(conn)
    with kbc.write_txn(conn):
        if event_first:
            kb._append_event(conn, tid, "completed", {})
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        if not event_first:
            kb._append_event(conn, tid, "completed", {})
    event = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()[0]
    assert status_ops(conn, tid)[0]["status_origin"] == event


def test_later_transaction_cannot_rebind_a_stale_wake(board):
    conn, _ = board
    tid = fresh(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    original = status_ops(conn, tid)[0]
    assert original["status_origin"] == ""
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completed", {})
    assert status_ops(conn, tid)[0]["status_origin"] == ""
    assert status_ops(conn, tid)[1]["status_origin"]


def test_rollback_and_nested_savepoint_do_not_capture_abandoned_status(board):
    conn, _ = board
    tid = fresh(conn)
    with pytest.raises(ValueError), kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completed", {})
        raise ValueError("rollback")
    assert status_ops(conn, tid) == []
    with kbc.write_txn(conn):
        with pytest.raises(ValueError), kbc.write_txn(conn, allow_nested=True):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            raise ValueError("savepoint")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert [r["status_origin"] for r in status_ops(conn, tid)] == [""]


def test_multiple_status_intents_and_autocommit_remain_explicitly_legacy(board):
    conn, _ = board
    tid = fresh(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completed", {})
    assert [r["status_origin"] for r in status_ops(conn, tid)] == [None, None]
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    assert status_ops(conn, tid)[-1]["status_origin"] is None


def test_manual_requeue_is_not_a_new_status_capture(board):
    from hermes_cli import kanban_db_sync as sync
    conn, _ = board
    tid = fresh(conn)
    with kbc.write_txn(conn):
        kb._append_event(conn, tid, "completed", {})
        conn.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                     "VALUES (?, 'tasks', ?, 'upsert', ?, '', 1)", (sync.uuid7(), tid, json.dumps({"status": "ready"})))
    assert status_ops(conn, tid)[0]["status_origin"] is None


def test_status_provenance_invalidated_across_connections_and_task_delete(board):
    conn, home = board
    tid = kb.create_task(conn, title="cross-connection", assignee="worker")
    assert conn.execute("SELECT origin FROM kanban_sync_status_origins WHERE task_id=?", (tid,)).fetchone()[0] == ""
    other = kbc.connect(home / "kanban.db")
    try:
        other.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        assert not conn.execute("SELECT 1 FROM kanban_sync_status_origins WHERE task_id=?", (tid,)).fetchone()
        with kbc.write_txn(other):
            other.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert conn.execute("SELECT 1 FROM kanban_sync_status_origins WHERE task_id=?", (tid,)).fetchone()
        other.execute("DELETE FROM tasks WHERE id=?", (tid,))
        assert not conn.execute("SELECT 1 FROM kanban_sync_status_origins WHERE task_id=?", (tid,)).fetchone()
    finally:
        other.close()


def test_identity_replacement_revokes_capture_instead_of_transferring_it(board):
    conn, _ = board
    target = kb.create_task(conn, title="old identity")
    replacement = kb.create_task(conn, title="replacement")
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_status_origins").fetchone()[0] == 2
    conn.execute("PRAGMA recursive_triggers=OFF")
    # Raw sqlite3 writer: the board factory refuses REPLACE (PR4045).
    sqlite3.Connection.execute(conn, "UPDATE OR REPLACE tasks SET id=? WHERE id=?", (target, replacement))
    assert conn.execute("SELECT title FROM tasks WHERE id=?", (target,)).fetchone()[0] == "replacement"
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_status_origins").fetchone()[0] == 0

