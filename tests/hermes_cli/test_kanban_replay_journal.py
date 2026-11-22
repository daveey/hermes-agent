"""Durable W=1 allocator uses the caller's Board transaction, not another writer."""
import json
import sqlite3

import pytest


def binding():
    return dict(board_incarnation='board-incarnation', gateway_principal='principal-0001',
                journal_id='journal-0001', generation='1', settled='0',
                last_op_id=None, last_payload_sha256=None, pending=None)


def test_allocate_rollback_and_restart_keep_one_exact_successor(tmp_path):
    from hermes_cli import kanban_replay_journal as journal

    path = tmp_path / 'board.db'
    conn = sqlite3.connect(path)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        journal.initialize(conn, binding())
    conn.execute('BEGIN IMMEDIATE')
    first = journal.issue(conn, {'client_op_id': 'operation-0001', 'fields': {'n': 1.0}})
    assert json.loads(first)['replay']['sequence'] == '1'
    conn.rollback()
    assert journal.snapshot(conn)['pending'] is None
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        second = journal.issue(conn, {'client_op_id': 'operation-0002', 'fields': {'n': 1.0}})
    conn.close()
    conn = sqlite3.connect(path)
    assert journal.pending_wire(conn) == second
    assert journal.snapshot(conn)['pending']['op_id'] == 'operation-0002'
    with pytest.raises(ValueError, match='outstanding issued envelope'):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.issue(conn, {'client_op_id': 'operation-0003'})
    assert journal.pending_wire(conn) == second
    conn.close()


@pytest.mark.parametrize('action', ['initialize', 'issue', 'recover'])
def test_mutation_requires_caller_transaction(action):
    from hermes_cli import kanban_replay_journal as journal

    conn = sqlite3.connect(':memory:')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        journal.initialize(conn, binding())
    with pytest.raises(ValueError, match='caller transaction required'):
        if action == 'initialize':
            journal.initialize(conn, binding())
        elif action == 'issue':
            journal.issue(conn, {'client_op_id': 'operation-0001'})
        else:
            journal.recover(conn, {}, lambda verdict: None)
    assert not conn.in_transaction
    conn.close()


@pytest.mark.parametrize('defect', ['digest', 'wire', 'missing', 'ack', 'state'])
def test_corrupt_custody_never_retries_or_records(defect):
    from hermes_cli import kanban_replay_journal as journal
    from hermes_cli.kanban_replay_wire import freeze
    import rfc8785

    conn = sqlite3.connect(':memory:')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        journal.initialize(conn, binding())
        encoded = journal.issue(conn, {'client_op_id': 'operation-0001'})
    wire = json.loads(encoded)
    state = journal.snapshot(conn)
    mutate = {
        'digest': lambda: wire['replay'].update(payload_sha256='0' * 64),
        'wire': lambda: wire.update(client_op_id='operation-9999'),
        'ack': lambda: wire['replay'].update(ack=dict(sequence='1', op_id='operation-9999',
                                                    payload_sha256='0' * 64)),
        'state': lambda: state.update(settled='01'),
        'missing': lambda: None,
    }
    mutate[defect]()
    if defect in ('wire', 'ack'):
        wire = freeze(wire)
    if defect == 'ack':
        state['pending']['payload_sha256'] = wire['replay']['payload_sha256']
    with conn:
        conn.execute('UPDATE kanban_replay_sender1 SET wire=?,state=?',
                     (None if defect == 'missing' else rfc8785.dumps(wire), json.dumps(state)))
    remote = dict(binding(), mode='STRICT', session='1', h='0', a='0', last_verdict=None)
    recorded = []
    with pytest.raises(ValueError):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.recover(conn, remote, recorded.append)
    assert recorded == []
    conn.close()


@pytest.mark.parametrize('intent,expected', [
    ({'client_op_id': 'bad'}, 'identity'),
    ({'client_op_id': 'operation-0001', 'replay': {}}, 'already framed'),
])
def test_invalid_intent_never_allocates(intent, expected):
    from hermes_cli import kanban_replay_journal as journal

    conn = sqlite3.connect(':memory:')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        journal.initialize(conn, binding())
    with pytest.raises(ValueError, match=expected):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.issue(conn, intent)
    assert journal.snapshot(conn) == binding()
    assert journal.pending_wire(conn) is None
    conn.close()


def test_two_connections_cannot_allocate_two_successors(tmp_path):
    from hermes_cli import kanban_replay_journal as journal

    path = tmp_path / 'board.db'
    first, second = sqlite3.connect(path), sqlite3.connect(path, timeout=0)
    with first:
        first.execute('BEGIN IMMEDIATE')
        journal.initialize(first, binding())
    first.execute('BEGIN IMMEDIATE')
    issued = journal.issue(first, {'client_op_id': 'operation-0001'})
    second.execute('BEGIN')
    with pytest.raises(sqlite3.OperationalError, match='locked'):
        journal.issue(second, {'client_op_id': 'operation-0002'})
    second.rollback()
    first.commit()
    second.execute('BEGIN')
    with pytest.raises(ValueError, match='outstanding issued envelope'):
        journal.issue(second, {'client_op_id': 'operation-0002'})
    second.rollback()
    assert journal.pending_wire(second) == issued
    first.close()
    second.close()


@pytest.mark.parametrize('settled,op_id,expected', [
    (str(2**63-1), 'operation-0002', 'pending sequence'),
    ('1', 'operation-0001', 'reuses settled'),
])
def test_exhaustion_and_identity_reuse_cannot_allocate(settled, op_id, expected):
    from hermes_cli import kanban_replay_journal as journal

    conn = sqlite3.connect(':memory:')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        journal.initialize(conn, binding())
        state = dict(binding(), settled=settled, last_op_id='operation-0001', last_payload_sha256='0' * 64)
        conn.execute('UPDATE kanban_replay_sender1 SET state=?', (json.dumps(state),))
    with pytest.raises(ValueError, match=expected):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.issue(conn, {'client_op_id': op_id})
    assert journal.snapshot(conn) == state
    conn.close()


def test_callback_failure_cannot_commit_partial_bookkeeping():
    from hermes_cli import kanban_replay_journal as journal

    conn = sqlite3.connect(':memory:')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        journal.initialize(conn, binding())
        conn.execute('CREATE TABLE outcomes(verdict TEXT)')
        wire = json.loads(journal.issue(conn, {'client_op_id': 'operation-0001'}))
    remote = dict(binding(), mode='STRICT', session='1', h='1', a='0', last_op_id='operation-0001',
                  last_payload_sha256=wire['replay']['payload_sha256'],
                  last_verdict=json.dumps({'client_op_id': 'operation-0001', 'status': 'applied'}))

    def fail(verdict):
        conn.execute('INSERT INTO outcomes VALUES (?)', (json.dumps(verdict),))
        raise RuntimeError('bookkeeping failed')

    with conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(RuntimeError, match='bookkeeping failed'):
            journal.recover(conn, remote, fail)
        assert conn.in_transaction
    assert conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0] == 0
    assert journal.snapshot(conn)['settled'] == '0'
    assert journal.pending_wire(conn) is not None
    conn.close()


def test_recovered_verdict_bookkeeping_and_custody_commit_together(tmp_path):
    from hermes_cli import kanban_replay_journal as journal

    conn = sqlite3.connect(tmp_path / 'board.db')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        journal.initialize(conn, binding())
        conn.execute('CREATE TABLE outcomes (verdict TEXT)')
        wire = json.loads(journal.issue(conn, {'client_op_id': 'operation-0001'}))
    remote = dict(binding(), mode='STRICT', session='1', h='1', a='0',
                  last_op_id='operation-0001', last_payload_sha256=wire['replay']['payload_sha256'],
                  last_verdict=json.dumps({'client_op_id': 'operation-0001', 'status': 'rejected'}))

    def record(verdict):
        conn.execute('INSERT INTO outcomes VALUES (?)', (json.dumps(verdict),))

    with pytest.raises(RuntimeError, match='lost commit'):
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            journal.recover(conn, remote, record)
            raise RuntimeError('lost commit')
    assert journal.pending_wire(conn) is not None
    assert conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0] == 0
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        assert journal.recover(conn, remote, record)['action'] == 'record'
    assert journal.pending_wire(conn) is None
    assert journal.snapshot(conn)['settled'] == '1'
    assert journal.snapshot(conn)['last_verdict'] == remote['last_verdict']
    assert json.loads(conn.execute('SELECT verdict FROM outcomes').fetchone()[0])['status'] == 'rejected'
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        successor = json.loads(journal.issue(conn, {'client_op_id': 'operation-0002'}))
    assert successor['replay']['sequence'] == '2'
    assert successor['replay']['ack'] == dict(sequence='1', op_id='operation-0001',
                                             payload_sha256=wire['replay']['payload_sha256'])
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        assert journal.recover(conn, remote, record)['action'] == 'retry'
    assert conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0] == 1
    conn.close()
