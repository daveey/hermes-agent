import pytest
from hermes_cli import kanban_db_connect as dbc, kanban_home_transfer as transfer, kanban_protect_transport as protect
from tests.hermes_cli import test_kanban_home_transfer_fence as support
board = support.board
seed, prepare = support.seed, support.prepare


def test_prepare_refuses_pending_protect_custody(board):
    task = seed(board)
    args = {'source_event_uid': 'source-event', 'source_run_uid': 'source-run', 'expected_status': 'ready',
            'expected_current_run_uid': None, 'expected_updated_at': 1, 'expected_episode': None,
            'requested_owner': 'actor-fixture', 'next_step': 'Resolve scope', 'recovery_trigger': 'Scoped decision',
            'escalate_at': 1000, 'evidence': ['isolated capture fixture']}
    payload = {'action': 'protect_stopped', 'board': 'fixture', 'home': 'worker-a3', 'request_uid': 'pending-protect-request',
               'target_task_id': task, 'caller': {'task_id': 'actor-fixture', 'run_uid': 'actor-run'}, 'args': args}
    with dbc.write_txn(board):
        board.execute("UPDATE kanban_sync_state SET mode='primary'")
        owned = protect.reserve(board, 'fixture', payload, 'request-event')
    assert owned['delivery_state'] == 'pending'
    assert board.execute('SELECT count(*) FROM kanban_sync_outbox').fetchone()[0] == 0
    with dbc.write_txn(board):
        with pytest.raises(ValueError, match='drained'):
            prepare(board, task)
