"""Custody is a transactional sweep input, independent of task writes. [daveey]"""
import pytest

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_generation as generation
from hermes_cli import kanban_home_revision as revision
from hermes_cli import kanban_sweep_gate as gate
from tests.hermes_cli import test_home_release_candidates as support

board = support.board


def test_pending_revision_is_committed_and_peer_visible(board):
    task = support.card(board, 'ready')
    calls = []
    run = lambda: gate.gated('custody-control', board, lambda: calls.append(1))
    gate.reset()
    run()
    run()
    assert len(calls) == 1
    before = revision.read(board)
    with pytest.raises(RuntimeError, match='rollback'):
        with dbc.write_txn(board):
            generation.mirror_feed(board, task, {'home_generation': 'revision-control-0001'}, 8)
            assert revision.read(board) != before
            raise RuntimeError('rollback')
    assert revision.read(board) == before
    run()
    assert len(calls) == 1
    path = board.execute('PRAGMA database_list').fetchone()[2]
    from pathlib import Path
    with dbc.connect_closing(Path(path)) as peer:
        with dbc.write_txn(peer):
            generation.mirror_feed(peer, task, {'home_generation': 'revision-control-0001'}, 8)
    run()
    assert len(calls) == 2
    run()
    assert len(calls) == 2
    with dbc.write_txn(board):
        generation.mirror_feed(board, task, {'home_generation': 'revision-control-0001',
                                           'home_transfer_pending': None}, 9)
    run()
    assert len(calls) == 3


@pytest.mark.parametrize('broken', ['absent', 'wrong'])
def test_unknown_custody_trigger_coverage_disables_cache(board, broken):
    with dbc.write_txn(board):
        board.execute('DROP TRIGGER home_revision1_kanban_home_generations1_update')
        if broken == 'wrong':
            board.execute('CREATE TRIGGER home_revision1_kanban_home_generations1_update '
                          'AFTER UPDATE ON kanban_home_generations1 BEGIN SELECT 1; END')
    assert revision.read(board) is None
    assert gate.key(board) is None
    calls = []
    for _ in range(2):
        gate.gated('unknown-custody', board, lambda: calls.append(1))
    assert len(calls) == 2
