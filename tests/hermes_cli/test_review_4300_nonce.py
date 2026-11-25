import pytest
from hermes_cli import kanban_db_connect as dbc, kanban_home_transfer as transfer
from tests.hermes_cli import test_kanban_home_source_stop as support
board = support.board


def test_aborted_nonce_cannot_be_reused_for_new_version(board):
    tid, record = support.prepared(board)
    with dbc.write_txn(board):
        transfer.abort_source(board, {**record, 'phase': 'aborted', 'rows': []})
        board.execute('UPDATE tasks SET sync_version=8 WHERE id=?', (tid,))
        board.execute('DELETE FROM kanban_sync_outbox')
    with dbc.write_txn(board), pytest.raises(ValueError):
        transfer.prepare(board, tid, source=record['source'], destination=record['destination'],
                         nonce=record['nonce'], expected_version=8)
