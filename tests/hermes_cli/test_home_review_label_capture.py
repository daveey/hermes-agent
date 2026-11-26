"""Real late memo and label-delivery writers refuse local custody. [daveey]"""
import pytest

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_memo as memo
from hermes_cli import kanban_operator_label_delivery as delivery
from hermes_cli import kanban_operator_labels as labels
from tests.hermes_cli import test_home_release_candidates as support

board = support.board


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', None])
def test_foreign_memo_new_reservation_checks_local_custody(board, phase):
    task = support.card(board, 'ready')
    support.freeze(board, task, phase)
    op = {'op': 'upsert', 'fields': {'task_id': task}}
    if phase:
        with pytest.raises(ValueError, match='home transfer'):
            with dbc.write_txn(board):
                memo.reserve(board, 'new-intent', task, op, foreign=True)
        assert memo.get(board, 'new-intent') is None
    else:
        with dbc.write_txn(board):
            assert memo.reserve(board, 'new-intent', task, op, foreign=True) == op
        assert memo.get(board, 'new-intent') == op


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', None])
def test_real_label_push_refuses_before_capture_or_network(board, monkeypatch, phase):
    task = support.card(board, 'ready')
    with dbc.write_txn(board):
        for statement in labels.SCHEMA:
            board.execute(statement)
    support.freeze(board, task, phase)
    seen = []

    class Peer:
        def __init__(self, ident):
            seen.append('construct')

        def row_evidence(self, name, table, key):
            seen.append('evidence')
            return {'state': 'live', 'row': {'id': task}}

        def ops(self, name, ops):
            seen.append('send')
            return {'results': [{'client_op_id': ops[0]['client_op_id'], 'status': 'rejected',
                                 'reason': 'fixture-only-no-network'}]}

        def close(self):
            seen.append('close')

    monkeypatch.setattr(delivery.client, 'load_identity', lambda: object())
    monkeypatch.setattr(delivery.client, 'Hub', Peer)
    call = lambda: delivery.push_event(board, 'fixture', 'worker-a3', task, 'new-label-event', {})
    if phase:
        with pytest.raises(ValueError, match='home transfer'):
            call()
        assert not seen
        assert board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_label_capture1'").fetchone() is None
        assert memo.get(board, 'new-label-event') is None
    else:
        assert call()[0] == 'rejected'
        assert seen == ['construct', 'evidence', 'send', 'close']
        assert memo.get(board, 'new-label-event') is not None
