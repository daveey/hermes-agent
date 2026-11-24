"""Only a scoped durable Board negative decision releases a preparation. [daveey]"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as source
from tests.hermes_cli import test_kanban_home_source_stop as support

board = support.board


def decision(reply):
    return {**reply, 'phase': 'aborted', 'rows': []}


def test_abort_retains_history_and_probes_and_survives_reopen(board):
    task, reply = support.prepared(board)
    before = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
    with dbc.write_txn(board):
        source.abort_source(board, decision(reply))
    assert not source.frozen(board, task)
    assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == before
    assert board.execute('SELECT cmd FROM task_wake_checks WHERE task_id=?', (task,)).fetchone()[0] == 'source-only-probe'
    path = board.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as other:
        other.row_factory = sqlite3.Row
        other.execute('BEGIN IMMEDIATE')
        source.abort_source(other, decision(reply))
        other.commit()
        assert not source.frozen(other, task)
    kb.add_comment(board, task, 'actor', 'ordinary work after proven noncommit')


@pytest.mark.parametrize('change', [{'nonce': 'wrong-nonce-000000'}, {'expected_version': True},
    {'source': 'worker-a2'}, {'destination': 'worker-a2'}, {'phase': 'prepared'}, {'phase': 'complete'}, {'rows': [{}]}])
def test_unknown_or_wrong_scope_never_unfreezes(board, change):
    task, reply = support.prepared(board)
    with dbc.write_txn(board), pytest.raises(ValueError):
        source.abort_source(board, {**decision(reply), **change})
    assert source.frozen(board, task)
    with pytest.raises(sqlite3.IntegrityError, match='home transfer prepared'):
        kb.add_comment(board, task, 'actor', 'still frozen')


def test_old_abort_cannot_release_new_attempt_or_authorize_stop(board):
    task, reply = support.prepared(board)
    with dbc.write_txn(board):
        source.abort_source(board, decision(reply))
        fresh = {**decision(reply), 'nonce': 'fresh-retry-0000001'}
        source.prepare(board, task, **{k: fresh[k] for k in ('source','destination','nonce','expected_version')})
    assert source.frozen(board, task)
    with dbc.write_txn(board), pytest.raises(ValueError):
        source.abort_source(board, decision(reply))
    assert source.frozen(board, task)
    with dbc.write_txn(board), pytest.raises(ValueError):
        source.finalize_source(board, reply)
    assert board.execute('SELECT created_on FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'worker-a3'


def test_abort_custody_is_immutable_and_cannot_finalize_after_abort(board):
    task, reply = support.prepared(board)
    with dbc.write_txn(board):
        source.abort_source(board, decision(reply))
    with dbc.write_txn(board), pytest.raises(ValueError):
        source.finalize_source(board, reply)
    for query in ['DELETE FROM kanban_home_aborted1', "UPDATE kanban_home_aborted1 SET receipt='{}'"]:
        with dbc.write_txn(board), pytest.raises(sqlite3.IntegrityError, match='immutable'):
            board.execute(query)
    assert not source.frozen(board, task)


@pytest.mark.parametrize('fault', ['missing_rows', 'wrong_scope', 'wrong_nonce', 'boolean_version'])
def test_partial_sql_custody_cannot_release_a_preparation(board, fault):
    task, reply = support.prepared(board)
    receipt = decision(reply)
    if fault == 'missing_rows':
        del receipt['rows']
    elif fault == 'wrong_scope':
        receipt['source'] = 'worker-a2'
    elif fault == 'wrong_nonce':
        receipt['nonce'] = 'foreign-nonce-000001'
    else:
        receipt['expected_version'] = True
    with dbc.write_txn(board), pytest.raises(sqlite3.IntegrityError):
        board.execute('INSERT INTO kanban_home_aborted1(task_id,nonce,receipt) VALUES (?,?,?)',
                      (task, reply['nonce'], json.dumps(receipt)))
    assert source.frozen(board, task)


def test_abort_never_releases_a_finalized_source(board):
    _task, reply = support.prepared(board)
    with dbc.write_txn(board):
        source.finalize_source(board, reply)
    with dbc.write_txn(board), pytest.raises(ValueError, match='already finalized'):
        source.abort_source(board, decision(reply))
    assert board.execute('SELECT COUNT(*) FROM kanban_home_aborted1').fetchone()[0] == 0
