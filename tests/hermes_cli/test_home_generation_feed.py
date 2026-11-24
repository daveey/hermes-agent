"""Trusted Board feed binds later native intent, never already pending intent."""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_sync as sync, kanban_db_connect as kbc
from tests.hermes_cli import test_home_transfer_capture as support

board = support.board


def feed(conn, task, version=8, nonce="committed-nonce-0001", **extra):
    fields: dict = dict(id=task, title="canonical", status="scheduled", created_at=1,
                  created_on="hub", home_generation=nonce, **extra)
    fields.setdefault("home_transfer_pending", None)  # this helper models a completed transfer
    change = sync.RemoteChange(seq=version, table="tasks", uid=task, op="upsert", fields=fields,
                               version=version, hlc="1-0-server", origin_gateway="server")
    sync.apply_remote_changes(conn, [change], new_cursor=version, observers=False)


def epoch(conn, task):
    row = conn.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone()
    return row[0] if row else None


def test_feed_epoch_drives_fresh_capture_without_upgrading_pending(board):
    task = kb.create_task(board, title="source")
    kb.add_comment(board, task, "actor", "old intent")
    old = board.execute("SELECT op_id,home_generations FROM kanban_sync_outbox WHERE table_name='task_comments'").fetchone()
    feed(board, task)
    assert epoch(board, task) == "committed-nonce-0001"
    assert board.execute("SELECT home_generations FROM kanban_sync_outbox WHERE op_id=?", (old[0],)).fetchone()[0] is None
    kb.add_comment(board, task, "actor", "new intent")
    row = board.execute("SELECT home_generations FROM kanban_sync_outbox WHERE table_name='task_comments' ORDER BY rowid DESC").fetchone()
    assert json.loads(row[0]) == {task: "committed-nonce-0001"}
    assert "home_generation" not in json.loads(board.execute("SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' LIMIT 1").fetchone()[0])


def test_old_feed_and_equal_version_conflict_cannot_replace_epoch(board):
    task = kb.create_task(board, title="source")
    feed(board, task)
    feed(board, task, 7, "older-nonce-000002")
    assert epoch(board, task) == "committed-nonce-0001"
    with pytest.raises(ValueError, match="generation"):
        feed(board, task, 8, "conflict-nonce-0002")
    assert epoch(board, task) == "committed-nonce-0001"
    assert board.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == 7
    feed(board, task, 9, "later-nonce-000003")
    assert epoch(board, task) == "later-nonce-000003"


@pytest.mark.parametrize("nonce", [True, 7, "", {}, "bad nonce"])
def test_malformed_generation_rolls_back_whole_feed_page(board, nonce):
    task = kb.create_task(board, title="original")
    with pytest.raises(ValueError, match="generation"):
        feed(board, task, nonce=nonce)
    assert epoch(board, task) is None
    assert kb.get_task(board, task).title == "original"


def test_epoch_retained_across_reopen_and_task_eviction(board):
    task = kb.create_task(board, title="source")
    feed(board, task)
    with kbc.write_txn(board):
        board.execute("UPDATE kanban_sync_state SET applying=1")
        board.execute("DELETE FROM tasks WHERE id=?", (task,))
        board.execute("UPDATE kanban_sync_state SET applying=0")
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as peer:
        assert peer.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone()[0] == "committed-nonce-0001"
    with pytest.raises(ValueError, match="generation"):
        feed(board, task, 7, "older-nonce-000002")
    assert board.execute("SELECT 1 FROM tasks WHERE id=?", (task,)).fetchone() is None


def test_own_echo_mirrors_epoch_without_reapplying_business_row(board):
    task = kb.create_task(board, title="local value must survive echo")
    op = board.execute("SELECT op_id FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (task,)).fetchone()[0]
    change = sync.RemoteChange(seq=8, table="tasks", uid=task, op="upsert",
        fields=dict(id=task, title="different echoed title", home_generation="committed-nonce-0001", home_transfer_pending=None),
        version=8, hlc="1-0-server", origin_gateway="worker-a3", client_op_id=op)
    result = sync.apply_remote_changes(board, [change], new_cursor=8, observers=False)
    assert result.skipped_echo == 1
    assert epoch(board, task) == "committed-nonce-0001"
    assert kb.get_task(board, task).title == "local value must survive echo"
    assert board.execute("SELECT 1 FROM kanban_sync_outbox WHERE op_id=?", (op,)).fetchone() is None
    kb.add_comment(board, task, "actor", "fresh post-echo intent")
    raw = board.execute("SELECT home_generations FROM kanban_sync_outbox WHERE table_name='task_comments'").fetchone()[0]
    assert json.loads(raw) == {task: "committed-nonce-0001"}
