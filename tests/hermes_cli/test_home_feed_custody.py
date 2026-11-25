"""Durable source-feed custody, replay, fairness and unchanged SQL fences."""
from __future__ import annotations

import json
import sqlite3

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_db_sync as sync
from hermes_cli import kanban_home_feed as feed, kanban_home_transfer as transfer
from tests.hermes_cli import test_kanban_home_source_stop as support
from tests.hermes_cli.test_review_home_feed_isolation import change

board = support.board


def handoff(conn, tid, record, seq, *, complete=False):
    return change(conn, tid, 9 if complete else 8, seq, created_on='hub',
                  home_generation=record['nonce'], home_transfer_pending=None if complete else record['nonce'])


def test_durable_exact_wire_replayed_by_real_idle_pull_after_stop(board, monkeypatch):
    from hermes_cli import kanban_sync_client as client
    task, record = support.prepared(board)
    cursor = sync.sync_state(board)['cursor']
    commit = handoff(board, task, record, cursor+1)
    complete = handoff(board, task, record, cursor+2, complete=True)
    before = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
    result = sync.apply_remote_changes(board, [commit, complete], new_cursor=cursor+2, observers=False)
    assert result.deferred == 2
    wires = [r[0] for r in board.execute('SELECT wire FROM kanban_home_feed1 ORDER BY id')]
    assert json.loads(wires[0])['change']['fields']['home_transfer_pending'] == record['nonce']
    assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == before
    with dbc.write_txn(board), pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        board.execute("UPDATE tasks SET title='still fenced' WHERE id=?", (task,))
    path = board.execute('PRAGMA database_list').fetchone()[2]
    # A new native connection, not an in-memory queue or the source caller's state.
    with dbc.connect_closing(Path(path)) as reopened:
        assert [r[0] for r in reopened.execute('SELECT wire FROM kanban_home_feed1 ORDER BY id')] == wires
        with dbc.write_txn(reopened):
            transfer.finalize_source(reopened, record)
        hub = client.Hub(client.Identity('http://127.0.0.1:1', 'fixture-key', 'worker-a3'))
        monkeypatch.setattr(client.acw, 'changes_page', lambda *a: dict(changes=[], next=cursor+2, more=False))
        monkeypatch.setattr(client, 'discard_replica_writes', lambda *a: None)
        pulled = client.pull(reopened, hub, 'default', observers=False)
        assert pulled == 0
        assert not feed.pending(reopened)
        assert reopened.execute('SELECT sync_version FROM tasks WHERE id=?', (task,)).fetchone()[0] == 9
        assert not transfer.frozen(reopened, task)
        assert not reopened.execute('SELECT 1 FROM kanban_sync_outbox').fetchone()
        # Replay once only, including on later idle polls.
        before_changes = reopened.total_changes
        client.pull(reopened, hub, 'default', observers=False)
        assert reopened.total_changes == before_changes


def test_atomic_capacity_failure_keeps_cursor_and_healthy_row_retryable(board, monkeypatch):
    task, record = support.prepared(board)
    peer = kb.create_task(board, title='unchanged')
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
    cursor = sync.sync_state(board)['cursor']
    monkeypatch.setattr(feed, 'MAX_ROWS', 0)
    rows = [change(board, peer, 4, cursor+1, title='remote'), handoff(board, task, record, cursor+2)]
    with pytest.raises(ValueError, match='custody capacity'):
        sync.apply_remote_changes(board, rows, new_cursor=cursor+2, observers=False)
    assert sync.sync_state(board)['cursor'] == cursor
    assert kb.get_task(board, peer).title == 'unchanged'
    assert not feed.pending(board)
    monkeypatch.setattr(feed, 'MAX_ROWS', 4096)
    sync.apply_remote_changes(board, rows, new_cursor=cursor+2, observers=False)
    assert kb.get_task(board, peer).title == 'remote'
    assert feed.pending(board)


def test_row_savepoint_fallback_retains_wire_not_partial_effects(board, monkeypatch):
    task, record = support.prepared(board)
    peer = kb.create_task(board, title='independent')
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
    cursor = sync.sync_state(board)['cursor']
    # Force the SQL fallback rather than relying solely on Python identity checks.
    monkeypatch.setattr(feed, 'blocked', lambda *a: False)
    result = sync.apply_remote_changes(board, [handoff(board, task, record, cursor+1),
                                             change(board, peer, 4, cursor+2, title='progress')],
                                       new_cursor=cursor+2, observers=False)
    assert result.deferred == 1 and kb.get_task(board, peer).title == 'progress'
    assert not board.execute('SELECT 1 FROM kanban_home_generations1 WHERE task_id=?', (task,)).fetchone()
    assert len(list(board.execute('SELECT wire FROM kanban_home_feed1'))) == 1


def test_unrelated_integrity_error_is_not_treated_as_transfer_deferral(board):
    task, record = support.prepared(board)
    peer = kb.create_task(board, title='independent')
    cursor = sync.sync_state(board)['cursor']
    with dbc.write_txn(board):
        board.execute("CREATE TRIGGER unrelated_fault BEFORE UPDATE ON tasks WHEN NEW.id='" + peer +
                      "' BEGIN SELECT RAISE(ABORT,'unrelated constraint'); END")
    with pytest.raises(sqlite3.IntegrityError, match='unrelated constraint'):
        sync.apply_remote_changes(board, [handoff(board, task, record, cursor+1),
                                         change(board, peer, 4, cursor+2, title='fail')],
                                  new_cursor=cursor+2, observers=False)
    assert sync.sync_state(board)['cursor'] == cursor
    assert not feed.pending(board)


def test_bounded_retry_does_not_starve_released_row_behind_frozen_row(board, monkeypatch):
    first, first_record = support.prepared(board)
    second = kb.create_task(board, title='second')
    with dbc.write_txn(board):
        board.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (second,))
        board.execute('DELETE FROM kanban_sync_outbox')
        record = support.record(board, second)
        record['nonce'] = 'second-replay-000001'
        transfer.prepare(board, second, source='worker-a3', destination='hub', nonce=record['nonce'], expected_version=7)
    cursor = sync.sync_state(board)['cursor']
    sync.apply_remote_changes(board, [handoff(board, first, first_record, cursor+1),
                                     handoff(board, second, record, cursor+2)], new_cursor=cursor+2, observers=False)
    with dbc.write_txn(board):
        transfer.finalize_source(board, record)
    monkeypatch.setattr(feed, 'REPLAY_LIMIT', 1)
    feed.drain(board, observers=False)
    assert board.execute('SELECT count(*) FROM kanban_home_feed1').fetchone()[0] == 2
    feed.drain(board, observers=False)
    assert board.execute('SELECT count(*) FROM kanban_home_feed1').fetchone()[0] == 1
    assert json.loads(board.execute('SELECT wire FROM kanban_home_feed1').fetchone()[0])['change']['uid'] == first
    assert transfer.frozen(board, first)


@pytest.mark.parametrize('kind', ['delete', 'child', 'link'])
def test_related_identity_is_deferred_including_deletes(board, kind):
    task, _record = support.prepared(board)
    cursor = sync.sync_state(board)['cursor']
    if kind == 'delete':
        ch = sync.RemoteChange(cursor+1, 'tasks', task, 'delete', {}, 8, '2000-0-server', 'server')
    else:
        fields = dict(task_id=task, author='remote', body='retained', created_at=1) if kind == 'child' else dict(parent_id=task, child_id=task)
        ch = sync.RemoteChange(cursor+1, 'task_comments' if kind == 'child' else 'task_links',
                               'deferred-child-0001', 'upsert', fields, 1, '2000-0-server', 'server')
    result = sync.apply_remote_changes(board, [ch], new_cursor=cursor+1, observers=False)
    assert result.deferred == 1
    assert json.loads(board.execute('SELECT wire FROM kanban_home_feed1').fetchone()[0])['change']['op'] == ch.op
    assert kb.get_task(board, task) is not None
