"""A lost admission reply must not keep a dead, never-bound attempt claimed."""
import os

import pytest

from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_owner_recovery import owner_attempt_provably_over
from tests.hermes_cli import test_kanban_owner_receipt_reclaim as helpers

board = helpers.board


def _refusal(board, tid, run, **changes):
    row = board.state(tid)
    payload = dict(pid=row['worker_pid'], claim_lock=row['claim_lock'], exit_code=69, submitter=True)
    payload.update(changes)
    board.event(tid, run, 'worker_result', payload, helpers.NOW)
    board.conn.commit()


@pytest.mark.parametrize('admission', [('queued', None), None, ('unknown', None)])
def test_dead_unbound_submitter_releases_even_with_live_owner(board, admission):
    run = board.dead_attempt('t_refusal', owner_pid=os.getpid(), admission=admission,
                             worker_fingerprint=dispatch._process_fingerprint(os.getpid()),
                             expires_age_seconds=-900)
    _refusal(board, 't_refusal', run)
    assert owner_attempt_provably_over(board.conn, 't_refusal')
    assert dispatch.detect_crashed_workers(board.conn) == ['t_refusal']
    row = board.state('t_refusal')
    assert (row['status'], row['claim_lock'], row['current_run_id']) == ('ready', None, None)
    assert dispatch.detect_crashed_workers(board.conn) == []


@pytest.mark.parametrize('changes', [
    {'pid': 1}, {'claim_lock': 'foreign:123'}, {'submitter': False}, {'exit_code': 1},
    {'exit_code': True},
])
def test_unattributable_refusal_keeps_claim(board, changes):
    run = board.dead_attempt('t_refusal', owner_pid=os.getpid(), admission=('queued', None))
    _refusal(board, 't_refusal', run, **changes)
    assert not owner_attempt_provably_over(board.conn, 't_refusal')
    assert dispatch.detect_crashed_workers(board.conn) == []


@pytest.mark.parametrize('variant', ['bound', 'malformed-bound', 'live', 'foreign', 'old-run'])
def test_refusal_does_not_override_execution_custody(board, variant):
    options = dict(owner_pid=os.getpid(), admission=('queued', None))
    if variant == 'bound':
        options['worker_bound'] = True
    elif variant == 'live':
        options.update(worker_pid=os.getpid(), worker_fingerprint=dispatch._process_fingerprint(os.getpid()))
    elif variant == 'foreign':
        options['lock_host'] = 'other-host'
    run = board.dead_attempt('t_refusal', **options)
    if variant == 'malformed-bound':
        board.event('t_refusal', run, 'worker_bound', {}, helpers.NOW)
    _refusal(board, 't_refusal', run + 100 if variant == 'old-run' else run)
    assert not owner_attempt_provably_over(board.conn, 't_refusal')
