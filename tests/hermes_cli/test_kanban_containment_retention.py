"""Retention, deletion and export over released containment history."""
from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as lc

board = lc.board


def _released_done(conn):
    relay, _consumer, authority, target, spec = lc.setup(conn)
    episode = lc.contain(conn, relay, target, spec)["episode"]
    assert lc.release(conn, target, episode, authority)["status"] == "ready"
    assert kb.claim_task(conn, relay, claimer="default") is not None
    assert kb.complete_task(conn, relay, summary="Finished after release")
    return relay


def _protected(conn, task):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN "
                        "('contained','containment_released')", (task,)).fetchone()[0]


def test_gc_events_prunes_board_and_retains_protected_history(board, monkeypatch):
    relay = _released_done(board)
    other = kb.create_task(board, title="Unrelated", assignee="default")
    assert kb.claim_task(board, other, claimer="default")
    assert kb.complete_task(board, other, summary="done")
    before = _protected(board, relay)
    assert before == 2
    cutoff = time.time() + 10
    monkeypatch.setattr(kb.time, "time", lambda: cutoff)
    assert kb.gc_events(board, older_than_seconds=0) > 0
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (other,)).fetchone()[0] == 0
    assert _protected(board, relay) == before


def test_released_task_deletes_with_its_history(board):
    from hermes_cli.kanban_db_archive import archive_task
    relay = _released_done(board)
    assert archive_task(board, relay)
    assert kb.delete_archived_task(board, relay)
    assert kb.get_task(board, relay) is None
    assert _protected(board, relay) == 0
    assert board.execute("SELECT COUNT(*) FROM containment_observations WHERE task_id=?", (relay,)).fetchone()[0] == 0


def test_active_containment_keeps_history_and_cannot_be_deleted(board):
    relay, _consumer, _authority, target, spec = lc.setup(board)
    lc.contain(board, relay, target, spec)
    target = target["task_id"]
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        with kb.write_txn(board):
            kb._delete_task_relations(board, target)
    assert _protected(board, target) == 1
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("DELETE FROM task_events WHERE task_id=?", (target,))
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        kb.delete_task(board, target)
    assert kb.get_task(board, target) is not None and _protected(board, target) == 1


def test_export_board_with_containment_triggers(board, tmp_path):
    from hermes_cli import kanban_transfer as transfer
    _released_done(board)
    assert transfer.export_board("default", tmp_path / "out.tar.gz")


def test_sync_delete_of_released_task_applies_and_active_one_is_refused(board, monkeypatch):
    from gateway.session_context import scoped_current_session_id
    from hermes_cli import kanban_containment_service as service
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_containment_connections import held
    from tests.hermes_cli import test_kanban_containment_remote as rm

    relay, authority, session, _hub = rm.setup(board, monkeypatch)
    child = board.execute("SELECT child_id FROM task_links WHERE parent_id=?", (relay,)).fetchone()[0]
    rm.adopt(board, monkeypatch, relay, session)
    with scoped_current_session_id(session):
        stop = service.contain(board, task_id=child, authority=authority, summary="Fixture")
    change = sync.RemoteChange(seq=1, table="tasks", uid=child, op="delete", fields={}, version=999,
                               hlc="", origin_gateway="home")
    cursor = sync.sync_state(board)["cursor"]
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        sync.apply_remote_changes(board, [change], new_cursor=cursor, board="fixture")
    assert kb.get_task(board, child) is not None and _protected(board, child) == 1
    rm.adopt(board, monkeypatch, authority, session)
    with scoped_current_session_id(session):
        assert service.resume(board, task_id=child, episode=stop["episode"])["status"] == "ready"
    assert not held(board, child)
    sync.apply_remote_changes(board, [change], new_cursor=cursor, board="fixture")
    assert kb.get_task(board, child) is None and _protected(board, child) == 0
