"""Native task candidate capture composes with immutable home custody epochs."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def capture_board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)


def seed(conn, predecessor, pending):
    task = 't_capture_composition'
    with kbc.write_txn(conn):
        if pending is not None:
            conn.execute('INSERT INTO kanban_home_generations1(task_id,nonce,pending) VALUES (?,?,?)',
                         (task, 'committed-nonce-0001', pending))
        if predecessor == 'outbox':
            conn.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('old-operation','tasks',?,'upsert','{}','',1)", (task,))
        elif predecessor == 'tombstone':
            conn.execute("INSERT INTO kanban_sync_tombstones(table_name,row_uid,deleted_at) VALUES ('tasks',?,1)", (task,))
        elif predecessor == 'parent':
            conn.execute('INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES (?,?)', (task, '{}'))
    return task


def insert(conn, task):
    conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES (?,'native intent','todo',1)", (task,))


@pytest.mark.parametrize('predecessor', ['none', 'outbox', 'tombstone', 'parent'])
@pytest.mark.parametrize('pending', [None, 0])
def test_candidate_capture_keeps_epoch_without_certifying_predecessors(capture_board, predecessor, pending):
    task = seed(capture_board, predecessor, pending)
    with kbc.write_txn(capture_board):
        insert(capture_board, task)
    row = dict(capture_board.execute('SELECT * FROM kanban_sync_outbox WHERE row_uid=? ORDER BY rowid DESC LIMIT 1',
                             (task,)).fetchone())
    expected = {task: 'committed-nonce-0001'} if pending == 0 else None
    assert (json.loads(row['home_generations']) if row['home_generations'] is not None else None) == expected
    assert row['identity_state'] is None
    if predecessor == 'none':
        assert (row['capture_protocol'], row['original_op_id'], row['original_created_at']) == (
            'capture_candidate1', row['op_id'], row['created_at'])
        assert json.loads(row['exposure_history']) == dict(
            protocol='exposure1', coverage='candidate', certified=False, exposures=[])
    else:
        assert tuple(row[key] for key in ('capture_protocol', 'original_op_id', 'original_created_at',
                                         'exposure_history')) == (None, None, None, None)
    with pytest.raises(sqlite3.IntegrityError, match='captured home generation is immutable'):
        with kbc.write_txn(capture_board):
            capture_board.execute('UPDATE kanban_sync_outbox SET home_generations=? WHERE op_id=?',
                          (json.dumps({task: 'different-nonce-0002'}), row['op_id']))


@pytest.mark.parametrize('predecessor', ['none', 'outbox', 'tombstone', 'parent'])
def test_pending_native_task_insert_refuses_atomically_even_when_caught(capture_board, predecessor):
    task = seed(capture_board, predecessor, 1)
    before = sorted(capture_board.iterdump())
    with kbc.write_txn(capture_board):
        with pytest.raises(sqlite3.IntegrityError, match='home transfer pending'):
            insert(capture_board, task)
    assert sorted(capture_board.iterdump()) == before
    with kbc.write_txn(capture_board):
        insert(capture_board, 't_healthy_capture_neighbor')
    row = capture_board.execute("SELECT capture_protocol,home_generations FROM kanban_sync_outbox "
                        "WHERE row_uid='t_healthy_capture_neighbor'").fetchone()
    assert tuple(row) == ('capture_candidate1', None)
