"""Journal loss cannot masquerade as first enrollment on a real sync database."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_replay_journal import binding


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    yield conn
    conn.close()


def test_initialize_wrong_board_never_claims_another_sync_row(board):
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match='journal already bound; recovery required'):
            journal.initialize(board, binding(), board_id='not-the-board')
    assert board.execute('SELECT journal_id FROM kanban_sync_state').fetchone()[0] is None


@pytest.mark.parametrize('field', ['settled', 'pending'])
def test_nonempty_initial_journal_is_refused_before_anchor_write(board, field):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    state = binding()
    if field == 'settled':
        state.update(settled='1', last_op_id='operation-0001', last_payload_sha256='0' * 64)
    else:
        state['pending'] = dict(sequence='1', op_id='operation-0001', payload_sha256='0' * 64)
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match='initial journal must be empty'):
            journal.initialize(board, state, board_id=board_id)
    assert board.execute('SELECT journal_id FROM kanban_sync_state').fetchone()[0] is None


def test_first_binding_persists_beside_outbox_and_survives_schema_reinstall(board):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    with kbc.write_txn(board):
        journal.initialize(board, binding(), board_id=board_id)
    assert board.execute('SELECT journal_id FROM kanban_sync_state').fetchone()[0] == binding()['journal_id']
    sync.apply_sync_schema_steps(board)
    assert journal.snapshot(board)['journal_id'] == binding()['journal_id']
    assert board.execute('SELECT board_id FROM kanban_replay_sender1').fetchone()[0] == board_id


def test_deleted_allocator_cannot_reset_retained_journal_identity(board):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    with kbc.write_txn(board):
        journal.initialize(board, binding(), board_id=board_id)
        journal.issue(board, {'client_op_id': 'operation-0001'})
    with kbc.write_txn(board):
        board.execute('DELETE FROM kanban_replay_sender1')
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match='journal already bound; recovery required'):
            journal.initialize(board, binding(), board_id=board_id)
    assert board.execute('SELECT COUNT(*) FROM kanban_replay_sender1').fetchone()[0] == 0
    assert board.execute('SELECT journal_id FROM kanban_sync_state').fetchone()[0] == binding()['journal_id']


@pytest.mark.parametrize('defect', ['missing', 'different', 'null'])
@pytest.mark.parametrize('action', ['snapshot', 'pending_wire', 'issue', 'recover'])
def test_divergent_sync_anchor_refuses_every_journal_entrypoint(board, defect, action):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    with kbc.write_txn(board):
        journal.initialize(board, binding(), board_id=board_id)
    with kbc.write_txn(board):
        if defect == 'missing':
            board.execute('DELETE FROM kanban_sync_state')
        else:
            board.execute('UPDATE kanban_sync_state SET journal_id=?',
                          (None if defect == 'null' else 'different-journal',))
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match='sync journal binding mismatch'):
            if action == 'issue':
                journal.issue(board, {'client_op_id': 'operation-0001'})
            elif action == 'recover':
                remote = dict(binding(), mode='STRICT', session='1', h='0', a='0', last_verdict=None)
                journal.recover(board, remote, lambda verdict: pytest.fail('recorded corrupt journal'))
            else:
                getattr(journal, action)(board)
    assert board.execute('SELECT wire FROM kanban_replay_sender1').fetchone()[0] is None


def test_restore_marker_persists_until_matching_remote_reconciliation(board):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    with kbc.write_txn(board):
        journal.initialize(board, binding(), board_id=board_id)
        journal.mark_restore_required(board)
    sync.apply_sync_schema_steps(board)
    assert board.execute('SELECT replay_restore_required FROM kanban_sync_state').fetchone()[0] == 1
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match='journal restore reconciliation required'):
            journal.issue(board, {'client_op_id': 'operation-0001'})
    remote = dict(binding(), mode='STRICT', session='1', h='0', a='0', last_verdict=None)
    with kbc.write_txn(board):
        journal.reconcile_restore(board, remote, lambda verdict: pytest.fail('unexpected verdict'))
    with kbc.write_txn(board):
        assert journal.issue(board, {'client_op_id': 'operation-0001'})


def test_initialize_failure_caught_inside_transaction_does_not_publish_anchor(board):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    with kbc.write_txn(board):
        journal.initialize(board, binding(), board_id=board_id)
    with kbc.write_txn(board):
        board.execute('UPDATE kanban_sync_state SET journal_id=NULL')
    with kbc.write_txn(board):
        with pytest.raises(sqlite3.IntegrityError):
            journal.initialize(board, dict(binding(), journal_id='replacement-journal'), board_id=board_id)
    assert board.execute('SELECT journal_id FROM kanban_sync_state').fetchone()[0] is None
    assert json.loads(board.execute('SELECT state FROM kanban_replay_sender1').fetchone()[0]) == binding()


@pytest.mark.parametrize('defect', ['identity', 'cursor', 'wire'])
def test_restore_fault_retains_hold_and_exact_pending_custody(board, defect):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    with kbc.write_txn(board):
        journal.initialize(board, binding(), board_id=board_id)
        wire = journal.issue(board, {'client_op_id': 'operation-0001'})
        journal.mark_restore_required(board)
    remote = dict(binding(), mode='STRICT', session='1', h='0', a='0', last_verdict=None)
    with kbc.write_txn(board):
        if defect == 'identity':
            remote['journal_id'] = 'different-journal'
        elif defect == 'cursor':
            remote['h'] = remote['a'] = '2'
        else:
            board.execute('UPDATE kanban_replay_sender1 SET wire=?', (b'{}',))
    before = board.execute('SELECT wire FROM kanban_replay_sender1').fetchone()[0]
    with kbc.write_txn(board):
        with pytest.raises(ValueError):
            journal.reconcile_restore(board, remote, lambda verdict: pytest.fail('unexpected verdict'))
    assert board.execute('SELECT replay_restore_required FROM kanban_sync_state').fetchone()[0] == 1
    assert board.execute('SELECT wire FROM kanban_replay_sender1').fetchone()[0] == before
    assert before == (b'{}' if defect == 'wire' else wire)


def test_successful_restore_reconciliation_rollback_keeps_hold(board):
    board_id = board.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    with kbc.write_txn(board):
        journal.initialize(board, binding(), board_id=board_id)
        journal.mark_restore_required(board)
    remote = dict(binding(), mode='STRICT', session='1', h='0', a='0', last_verdict=None)
    with pytest.raises(RuntimeError, match='rollback'):
        with kbc.write_txn(board):
            journal.reconcile_restore(board, remote, lambda verdict: pytest.fail('unexpected verdict'))
            raise RuntimeError('rollback')
    assert board.execute('SELECT replay_restore_required FROM kanban_sync_state').fetchone()[0] == 1
