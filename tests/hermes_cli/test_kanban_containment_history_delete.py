"""Exact-removal kills for released_history_delete permit predicates."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as lc

board = lc.board


def _released(conn):
    relay, _consumer, authority, target, spec = lc.setup(conn)
    episode = lc.contain(conn, relay, target, spec)["episode"]
    assert lc.release(conn, target, episode, authority)["status"] == "ready"
    assert not connections.held(conn, relay)
    return relay


def _protected(conn, task):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN ('contained','containment_released')",
        (task,),
    ).fetchone()[0]


def _delete_protected(conn, task):
    conn.execute(
        "DELETE FROM task_events WHERE task_id=? AND kind IN ('contained','containment_released')",
        (task,),
    )


def test_delete_permit_refuses_while_pending(board):
    relay, _consumer, _authority, target, spec = lc.setup(board)
    lc.contain(board, relay, target, spec)
    with kb.write_txn(board):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            with connections.released_history_delete(board, relay):
                pass
        assert board.execute("SELECT kanban_containment_delete(?)", (relay,)).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            _delete_protected(board, relay)
    assert _protected(board, relay) == 1


def test_delete_permit_without_verifier_keeps_released_history_held(board):
    task = _released(board)
    with kb.write_txn(board):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            with guard.released_history_delete(board, task):
                pass
        with connections.released_history_delete(board, task):
            _delete_protected(board, task)
    assert _protected(board, task) == 0


def test_delete_permit_udf_is_false_outside_transaction(board):
    task = _released(board)
    assert not board.in_transaction
    with connections.released_history_delete(board, task):
        assert not board.in_transaction
        assert board.execute("SELECT kanban_containment_delete(?)", (task,)).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            _delete_protected(board, task)
    assert _protected(board, task) == 2


def test_delete_permit_is_task_and_reset_scoped(board):
    granted, other = _released(board), _released(board)
    with kb.write_txn(board):
        with connections.released_history_delete(board, granted):
            assert board.execute("SELECT kanban_containment_delete(?)", (granted,)).fetchone()[0] == 1
            assert board.execute("SELECT kanban_containment_delete(?)", (other,)).fetchone()[0] == 0
            with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
                _delete_protected(board, other)
            _delete_protected(board, granted)
        assert board.execute("SELECT kanban_containment_delete(?)", (granted,)).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            _delete_protected(board, other)
    assert _protected(board, granted) == 0
    assert _protected(board, other) == 2


def test_delete_permit_is_connection_scoped(board):
    task = _released(board)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    peer = sqlite3.connect(path, isolation_level=None)
    try:
        connections.register(peer)
        with kb.write_txn(board):
            with connections.released_history_delete(board, task):
                assert board.execute("SELECT kanban_containment_delete(?)", (task,)).fetchone()[0] == 1
                peer.execute("BEGIN")
                try:
                    assert peer.in_transaction
                    assert peer.execute("SELECT kanban_containment_delete(?)", (task,)).fetchone()[0] == 0
                finally:
                    peer.execute("ROLLBACK")
    finally:
        peer.close()
    assert _protected(board, task) == 2


def test_delete_permit_resets_after_exception(board):
    task = _released(board)
    with kb.write_txn(board):
        with pytest.raises(RuntimeError, match="scope-reset"):
            with connections.released_history_delete(board, task):
                assert board.execute("SELECT kanban_containment_delete(?)", (task,)).fetchone()[0] == 1
                raise RuntimeError("scope-reset")
        assert board.execute("SELECT kanban_containment_delete(?)", (task,)).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            _delete_protected(board, task)
    assert _protected(board, task) == 2
    assert board.execute("SELECT COUNT(*) FROM containment_observations WHERE task_id=?", (task,)).fetchone()[0] >= 1


def test_delete_task_relations_does_not_cross_tasks(board):
    granted, other = _released(board), _released(board)
    with kb.write_txn(board):
        kb._delete_task_relations(board, granted)
    assert _protected(board, granted) == 0
    assert _protected(board, other) == 2
    assert board.execute("SELECT COUNT(*) FROM containment_observations WHERE task_id=?", (granted,)).fetchone()[0] == 0
    assert board.execute("SELECT COUNT(*) FROM containment_observations WHERE task_id=?", (other,)).fetchone()[0] >= 1
