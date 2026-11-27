"""Stop maintenance and command identities compose with home custody (PR4300 review).

Legacy stopped preparations remain immutable, but must not starve healthy sync.
New stopped preparations refuse before committing any custody. Command identity
rekeys cannot destroy an independently captured epoch through SQLite REPLACE.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_generation as generation
from hermes_cli import kanban_home_transfer as transfer
from hermes_cli import kanban_stop_guards as guards
from hermes_cli.kanban_stop_ended import KIND
from tests.hermes_cli import test_kanban_home_transfer_fence as support

board = support.board
NONCE = 'review-stop-epoch-0001'


def stopped(conn, *, token=111):
    task = support.seed(conn)
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage',worker_started_at=? WHERE id=?", (token, task))
        conn.execute('UPDATE kanban_sync_state SET applying=1')
        kb._append_event(conn, task, KIND, {'version': 1})
        conn.execute('UPDATE kanban_sync_state SET applying=0')
        guards.install(conn)
        conn.execute('DELETE FROM kanban_sync_outbox')
    return task


def freeze_legacy(conn, task, kind):
    with dbc.write_txn(conn):
        if kind == 'pending':
            generation.mirror_feed(conn, task, {'home_generation': NONCE, 'home_transfer_pending': NONCE}, 8)
        else:
            # Persisted pre-fix preparation, not permission for a new transfer.
            transfer._install(conn)
            conn.execute('INSERT INTO kanban_home_prepared1 VALUES (?,?,?,?,?)',
                         (task, 'worker-a3', 'hub', NONCE, 7))
            if kind == 'malformed':
                conn.execute('DROP TRIGGER home_prepared1_marker_update')
                conn.execute("UPDATE kanban_home_prepared1 SET nonce='invalid' WHERE task_id=?", (task,))


def token(conn, task):
    return conn.execute('SELECT worker_started_at FROM tasks WHERE id=?', (task,)).fetchone()[0]


@pytest.mark.parametrize('kind', ['prepared', 'pending', 'malformed'])
def test_sync_round_heals_neighbour_without_touching_legacy_frozen_stop(board, monkeypatch, kind):
    from hermes_cli import kanban_sync_client as client
    from hermes_cli import kanban_sync_rejections as rejections
    from hermes_cli import kanban_sync_reserved_key as reserved
    from hermes_cli import kanban_sync_round_send as rounds

    task, healthy = stopped(board), stopped(board)
    freeze_legacy(board, task, kind)
    before = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
    calls = []
    def push(*args, **kwargs):
        calls.append(token(board, healthy))
        return client.PushResult()
    monkeypatch.setattr(client, 'push', push)
    monkeypatch.setattr(reserved, 'advance', lambda *a: None)
    monkeypatch.setattr(rejections, 'resolve_rejections', lambda *a: 0)
    for _ in range(2):
        rounds.send(board, None, 'fixture', 'worker-a3', 'primary',
                    SimpleNamespace(push=None, rejection_resolutions=0), after_pull=False, before_head=False)
        assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == before
        assert token(board, healthy) is None
    assert calls == [None, None]
    assert transfer.frozen(board, task)


def test_unfrozen_stopped_tokens_still_heal(board):
    tasks = [stopped(board), stopped(board)]
    assert guards.clear_stale_worker_start(board) == 2
    assert [token(board, t) for t in tasks] == [None, None]
    assert guards.clear_stale_worker_start(board) == 0


@pytest.mark.parametrize('kind', ['prepared', 'pending', 'malformed'])
def test_sweep_rechecks_custody_at_writer_entry(board, monkeypatch, kind):
    task, healthy = stopped(board), stopped(board)
    real_writer = kb.write_txn
    captured = []

    @contextmanager
    def raced(conn, **kwargs):
        monkeypatch.setattr(kb, 'write_txn', real_writer)
        freeze_legacy(conn, task, kind)
        captured.append(tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()))
        with real_writer(conn, **kwargs):
            yield

    monkeypatch.setattr(kb, 'write_txn', raced)
    assert guards.clear_stale_worker_start(board) == 1
    assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == captured[0]
    assert token(board, healthy) is None


def test_only_frozen_sweep_does_not_reserve_a_writer(board, monkeypatch):
    task = stopped(board)
    freeze_legacy(board, task, 'pending')
    def unexpected_writer(*args, **kwargs):
        pytest.fail('all-frozen idle pass must be read-only')
    monkeypatch.setattr(kb, 'write_txn', unexpected_writer)
    assert guards.clear_stale_worker_start(board) == 0


@pytest.mark.parametrize('start_token', [None, 111])
def test_stopped_prepare_refuses_without_schema_or_custody_even_when_caught(board, start_token):
    task = stopped(board, token=start_token)
    before = sorted(board.iterdump())
    with dbc.write_txn(board):
        with pytest.raises(ValueError, match='administratively stopped'):
            support.prepare(board, task)
    assert sorted(board.iterdump()) == before
    assert not transfer.frozen(board, task)


@pytest.mark.parametrize('kind', ['prepared', 'pending'])
def test_stopped_token_cleanup_recovers_after_exact_custody_release(board, kind):
    task = stopped(board)
    freeze_legacy(board, task, kind)
    assert guards.clear_stale_worker_start(board) == 0
    with dbc.write_txn(board):
        if kind == 'prepared':
            transfer.abort_source(board, {'task': task, 'source': 'worker-a3', 'destination': 'hub',
                                         'nonce': NONCE, 'expected_version': 7, 'phase': 'aborted', 'rows': []})
        else:
            generation.mirror_feed(board, task, {'home_generation': NONCE, 'home_transfer_pending': None}, 9)
    assert not transfer.frozen(board, task)
    assert guards.clear_stale_worker_start(board) == 1
    assert token(board, task) is None
    assert guards.clear_stale_worker_start(board) == 0
    # The independent administrative stop is still enforced after home release.
    with pytest.raises(sqlite3.IntegrityError, match='administrative_stop_immutable'):
        with dbc.write_txn(board):
            board.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))


@pytest.mark.parametrize('column,value', [('run_uid', 'r1'), ('op_id', 'o1'), ('run_uid', 'r3')])
@pytest.mark.parametrize('mode', ['', 'OR REPLACE', 'OR IGNORE'])
def test_command_unique_identity_cannot_be_rekeyed(board, column, value, mode):
    from hermes_cli import kanban_failure_release as release
    task = support.seed(board)
    with dbc.write_txn(board):
        release._ensure_commands(board)
        board.execute('CREATE UNIQUE INDEX command_secondary_fixture ON kanban_failure_commands(op_id)')
        from hermes_cli import kanban_home_command as command
        command.install(board, 'kanban_failure_commands')
        board.execute('INSERT INTO kanban_failure_commands(run_uid,task_id,op_id,operation_kind,home_generations) '
                      'VALUES (?,?,?,?,?)', ('r1', task, 'o1', 'retain', '{"a":"epoch-old-0000000001"}'))
        board.execute('INSERT INTO kanban_failure_commands(run_uid,task_id,op_id,operation_kind,home_generations) '
                      'VALUES (?,?,?,?,?)', ('r2', task, 'o2', 'release', None))
    before = [tuple(r) for r in board.execute('SELECT * FROM kanban_failure_commands ORDER BY run_uid')]
    with pytest.raises(sqlite3.IntegrityError, match='command .* immutable'):
        with dbc.write_txn(board):
            board.execute(f"UPDATE {mode} kanban_failure_commands SET {column}=? WHERE run_uid='r2'", (value,))
    assert [tuple(r) for r in board.execute('SELECT * FROM kanban_failure_commands ORDER BY run_uid')] == before
    # Delivery bookkeeping and exact identity echoes are still ordinary writes.
    with dbc.write_txn(board):
        board.execute("UPDATE kanban_failure_commands SET run_uid=run_uid,op_id=op_id WHERE run_uid='r2'")
