"""Adjacent SQLite predicates are exercised independently of ingress validation."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as lc

board = lc.board


def _held(conn):
    relay, _consumer, _authority, target, spec = lc.setup(conn)
    lc.contain(conn, relay, target, spec)
    return relay


@pytest.mark.parametrize("column,value", [("current_run_id", 123456), ("completed_at", 123456)])
def test_authorized_triage_write_still_requires_no_run_or_completion(board, column, value):
    task = _held(board)
    with kb.write_txn(board):
        with guard.authorized_write(board, task):
            with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
                board.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, task))
    row = board.execute("SELECT current_run_id,completed_at FROM tasks WHERE id=?", (task,)).fetchone()
    assert tuple(row) == (None, None)


def test_unheld_update_remains_legal(board):
    task = kb.create_task(board, title="Fixture", assignee="default")
    board.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))
    assert kb.get_task(board, task).status == "todo"


def test_ordinary_event_cannot_be_renamed_into_protected_history(board):
    task = _held(board)
    with kb.write_txn(board):
        cursor = board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'fixture','{}',0)", (task,))
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute("UPDATE task_events SET kind='contained' WHERE id=?", (cursor.lastrowid,))


@pytest.mark.parametrize("column,value", [("payload", '{"changed":true}'), ("kind", "containment_released"), ("created_at", 99)])
def test_uid_backfill_requires_all_other_columns_unchanged(board, column, value):
    task = _held(board)
    with kb.write_txn(board):
        with guard.authorized_write(board, task):
            cursor = board.execute("INSERT INTO task_events(task_id,kind,payload,created_at,uid) VALUES (?,'contained','{}',0,NULL)", (task,))
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute(f"UPDATE task_events SET uid='fixture-uid',{column}=? WHERE id=?", (value, cursor.lastrowid))
        board.execute("UPDATE task_events SET uid='fixture-uid' WHERE id=?", (cursor.lastrowid,))


@pytest.mark.parametrize("column", ["uid", "task_id", "kind", "payload"])
def test_replica_event_permit_cannot_override_frozen_event_coordinates(board, column):
    task = _held(board)
    other = kb.create_task(board, title="Fixture peer", assignee="default")
    row = board.execute("SELECT id FROM task_events WHERE task_id=? AND kind='contained'", (task,)).fetchone()
    # A positive transport permit isolates the SQL equality clause from the
    # transport verifier, which independently rejects these mutations too.
    board.create_function("kanban_containment_sync_event", 4, lambda *_: 1)
    values = {"uid": "different", "task_id": other, "kind": "fixture", "payload": "{}"}
    with kb.write_txn(board):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute(f"UPDATE task_events SET {column}=? WHERE id=?", (values[column], row[0]))
        board.execute("UPDATE task_events SET created_at=created_at+1 WHERE id=?", (row[0],))


def test_replica_edge_permit_is_insert_only(board):
    task = _held(board)
    other = kb.create_task(board, title="Fixture peer", assignee="default")
    board.create_function("kanban_containment_sync_edge", 2, lambda *_: 1)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (task, other))
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (task, other))
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute("UPDATE task_links SET parent_id=? WHERE parent_id=? AND child_id=?", (other, task, other))


def test_attestation_insert_and_update_are_fenced(board):
    task = _held(board)
    values = (task, "fixture-receipt", "fixture-episode", "a" * 64, "b" * 64)
    with kb.write_txn(board):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute("INSERT INTO containment_attestations VALUES (?,?,?,?,?)", values)
        with guard.authorized_write(board, task):
            board.execute("INSERT INTO containment_attestations VALUES (?,?,?,?,?)", values)
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute("UPDATE containment_attestations SET episode_uid='changed' WHERE receipt_uid='fixture-receipt'")


def test_authorized_write_scope_restores_after_exception(board):
    task = _held(board)
    with kb.write_txn(board):
        with pytest.raises(RuntimeError, match="fixture"):
            with guard.authorized_write(board, task):
                assert board.execute("SELECT kanban_containment_write(?)", (task,)).fetchone()[0] == 1
                raise RuntimeError("fixture")
        assert board.execute("SELECT kanban_containment_write(?)", (task,)).fetchone()[0] == 0
        assert board.execute("SELECT kanban_containment_write('other')").fetchone()[0] == 0


def test_write_permit_expires_when_transaction_ends(board):
    task = _held(board)
    board.execute("BEGIN")
    with guard.authorized_write(board, task):
        assert board.execute("SELECT kanban_containment_write(?)", (task,)).fetchone()[0] == 1
        board.execute("COMMIT")
        assert board.execute("SELECT kanban_containment_write(?)", (task,)).fetchone()[0] == 0


def test_write_permit_is_bound_to_connection_and_task(board):
    task = _held(board)
    peer = sqlite3.connect(board.execute("PRAGMA database_list").fetchone()[2], isolation_level=None)
    try:
        guard.register(peer)
        with kb.write_txn(board):
            with guard.authorized_write(board, task):
                peer.execute("BEGIN")
                try:
                    assert peer.execute("SELECT kanban_containment_write(?)", (task,)).fetchone()[0] == 0
                    assert board.execute("SELECT kanban_containment_write('other')").fetchone()[0] == 0
                finally:
                    peer.execute("ROLLBACK")
    finally:
        peer.close()


def test_install_bootstraps_missing_marker_observation(board):
    task = _held(board)
    board.execute("DELETE FROM containment_observations WHERE task_id=?", (task,))
    guard.install(board)
    assert board.execute("SELECT 1 FROM containment_observations WHERE task_id=?", (task,)).fetchone()
