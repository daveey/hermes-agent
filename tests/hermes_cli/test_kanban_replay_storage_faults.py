"""Real SQLITE_FULL rollback at journal/exposure boundaries, not disk exhaustion."""
from contextlib import closing, contextmanager
import json
import multiprocessing
import signal
import sqlite3

import pytest

from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_exposure as exposure
from hermes_cli.kanban_sync_client import Identity
from tests.hermes_cli.test_kanban_replay_journal import binding, initialize


@pytest.fixture(params=['DELETE', 'WAL'])
def store(tmp_path, request):
    path = tmp_path / 'custody.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert conn.execute(f'PRAGMA journal_mode={request.param}').fetchone()[0].upper() == request.param
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        initialize(conn, journal)
        conn.execute('CREATE TABLE kanban_sync_outbox(op_id TEXT PRIMARY KEY, exposure_history TEXT)')
        conn.execute("INSERT INTO kanban_sync_outbox VALUES ('operation-0001',NULL)")
    yield conn, path
    conn.close()


def _limit(conn):
    pages = conn.execute('PRAGMA page_count').fetchone()[0]
    assert conn.execute('PRAGMA freelist_count').fetchone()[0] == 0
    assert conn.execute(f'PRAGMA max_page_count={pages}').fetchone()[0] == pages


@contextmanager
def _reopen(conn, path):
    conn.close()
    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    try:
        yield reopened
    finally:
        reopened.close()


def _full(exc):
    assert exc.value.sqlite_errorcode == sqlite3.SQLITE_FULL


def test_real_full_during_issue_does_not_allocate_or_lose_source(store):
    conn, path = store
    before = journal.snapshot(conn)
    _limit(conn)
    with pytest.raises(sqlite3.OperationalError) as error:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.issue(conn, dict(client_op_id='operation-0001', fields=dict(title='x' * 65536)))
    _full(error)
    with _reopen(conn, path) as reopened:
        assert journal.snapshot(reopened) == before
        assert journal.pending_wire(reopened) is None
        assert reopened.execute('SELECT op_id FROM kanban_sync_outbox').fetchone()[0] == 'operation-0001'
        # Space repaired: same unissued identity can allocate, never skip a slot.
        reopened.execute('BEGIN IMMEDIATE')
        wire = journal.issue(reopened, dict(client_op_id='operation-0001', fields=dict(title='x' * 65536)))
        assert json.loads(wire)['replay']['sequence'] == '1'
        reopened.commit()
        with closing(sqlite3.connect(path)) as reader:
            assert journal.pending_wire(reader) == wire
            assert journal.snapshot(reader)['pending']['op_id'] == 'operation-0001'


def test_real_full_during_exposure_keeps_exact_issued_bytes_and_unknown_history(store):
    conn, path = store
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        frozen = journal.issue(conn, dict(client_op_id='operation-0001', fields=dict(title='x' * 65536)))
    before = journal.snapshot(conn)
    _limit(conn)
    with pytest.raises(sqlite3.OperationalError) as error:
        exposure._observe(conn, [json.loads(frozen)], 'fixture-board', '/replay/ops',
                          Identity('https://example.invalid', 'fixture-key', 'fixture-gateway'),
                          dict(board_incarnation='board-incarnation', gateway_principal='principal-0001'))
    _full(error)
    with _reopen(conn, path) as reopened:
        assert journal.snapshot(reopened) == before
        assert journal.pending_wire(reopened) == frozen
        assert reopened.execute('SELECT exposure_history FROM kanban_sync_outbox').fetchone()[0] is None
        exposure._observe(reopened, [json.loads(frozen)], 'fixture-board', '/replay/ops',
                          Identity('https://example.invalid', 'fixture-key', 'fixture-gateway'),
                          dict(board_incarnation='board-incarnation', gateway_principal='principal-0001'))
        history = json.loads(reopened.execute('SELECT exposure_history FROM kanban_sync_outbox').fetchone()[0])
        assert history['coverage'] == 'unknown' and history['certified'] is False
        assert history['exposures'][0]['wire'] == json.loads(frozen)
        assert journal.pending_wire(reopened) == frozen


def test_real_full_after_source_prune_rolls_back_settlement_and_ack_metadata(store):
    conn, path = store
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        frozen = journal.issue(conn, dict(client_op_id='operation-0001'))
    before = journal.snapshot(conn)
    pending = before['pending']
    remote = dict(binding(), mode='STRICT', session='1', h='1', a='0',
                  last_op_id=pending['op_id'], last_payload_sha256=pending['payload_sha256'],
                  last_verdict=json.dumps(dict(client_op_id=pending['op_id'], status='applied', detail='x' * 65536)))
    pruned = []

    def record(verdict):
        conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (verdict['client_op_id'],))
        pruned.append(verdict['client_op_id'])

    _limit(conn)
    with pytest.raises(sqlite3.OperationalError) as error:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.recover(conn, remote, record)
    _full(error)
    assert pruned == ['operation-0001']  # Failure is after the actual source DELETE.
    with _reopen(conn, path) as reopened:
        assert journal.snapshot(reopened) == before
        assert journal.pending_wire(reopened) == frozen
        source = reopened.execute('SELECT op_id FROM kanban_sync_outbox').fetchone()
        assert source is not None, 'failed settlement lost source custody'
        assert source[0] == pending['op_id']
        def record_reopened(verdict):
            reopened.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (verdict['client_op_id'],))

        with reopened:
            reopened.execute('BEGIN IMMEDIATE')
            decision = journal.recover(reopened, remote, record_reopened)
        assert decision['action'] == 'record'
        after = journal.snapshot(reopened)
        assert after['pending'] is None and after['settled'] == '1'
        assert after['last_op_id'] == pending['op_id']
        assert after['last_payload_sha256'] == pending['payload_sha256']
        assert after['last_verdict'] == remote['last_verdict']
        assert reopened.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == 0


def _uncommitted(path, phase, ready):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        if phase == 'issue':
            journal.issue(conn, dict(client_op_id='operation-0001', fields=dict(title='x' * 65536)))
        else:
            pending = journal.snapshot(conn)['pending']
            remote = dict(binding(), mode='STRICT', session='1', h='1', a='0',
                          last_op_id=pending['op_id'], last_payload_sha256=pending['payload_sha256'],
                          last_verdict=json.dumps(dict(client_op_id=pending['op_id'], status='applied')))

            def record(verdict):
                conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (verdict['client_op_id'],))
                if phase == 'prune':
                    ready.send('pruned_uncommitted')
                    assert ready.poll(30), 'parent did not kill the owned child'
                    ready.recv()

            journal.recover(conn, remote, record)
        if phase == 'committed':
            conn.commit()
        ready.send('boundary_reached')
        assert ready.poll(30), 'parent did not kill the owned child'
        ready.recv()
    finally:
        conn.close()
        ready.close()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('phase', ['issue', 'prune', 'settle', 'committed'])
def test_hard_death_preserves_atomic_allocator_source_prune_and_ack_tuple(store, phase):
    conn, path = store
    if phase != 'issue':
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.issue(conn, dict(client_op_id='operation-0001', fields=dict(title='x' * 65536)))
    before = journal.snapshot(conn)
    frozen = journal.pending_wire(conn)
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_uncommitted, args=(path, phase, child))
    proc.start()
    child.close()
    try:
        assert parent.poll(30), 'child did not reach the actual SQLite boundary'
        assert parent.recv() == ('pruned_uncommitted' if phase == 'prune' else 'boundary_reached')
        assert proc.is_alive()
        proc.kill()
        proc.join(10)
        assert proc.exitcode == -getattr(signal, "SIGKILL", signal.SIGTERM)
        with _reopen(conn, path) as reopened:
            if phase == 'committed':
                after = journal.snapshot(reopened)
                assert after['pending'] is None and after['settled'] == '1'
                assert after['last_op_id'] == before['pending']['op_id']
                assert after['last_payload_sha256'] == before['pending']['payload_sha256']
                assert json.loads(after['last_verdict'])['client_op_id'] == after['last_op_id']
                assert reopened.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == 0
                assert journal.pending_wire(reopened) is None
            else:
                assert journal.snapshot(reopened) == before
                assert journal.pending_wire(reopened) == frozen
                assert reopened.execute('SELECT op_id FROM kanban_sync_outbox').fetchone()[0] == 'operation-0001'
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(10)
        parent.close()
