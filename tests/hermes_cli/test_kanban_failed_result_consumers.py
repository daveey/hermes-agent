"""Every classifier must consume the same causal modern receipt."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_owner_recovery as owner
from hermes_cli import kanban_owner_storage_refusal as storage
from hermes_cli import kanban_quota_receipt as quota
from hermes_cli import kanban_worker_log as logs
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board
STORAGE = 'Worker failed: WorkerPersistenceError: storage_unavailable'


def _history(board, latest, *, ambiguous=False):
    conn, context = board
    first = fixtures._failed_child(conn, context)
    payload = json.loads(first['payload'])
    second = {**payload, 'result_sequence': 1 if ambiguous else 2, **latest}
    with kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'worker_result', second, run_id=context['run_id'])
        conn.execute('UPDATE task_events SET id=? WHERE id=?', (first['id'] + 1000, first['id']))
    return conn, context, payload


@pytest.mark.parametrize('failed_results_only', [False, True])
def test_reclaim_classification_follows_causal_receipt(board, failed_results_only):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, ctx, _ = _history(board, {'exit_code': 7, 'last_output': 'causal final output'})
    dispatch.detect_crashed_workers(conn, failed_results_only=failed_results_only)
    run = conn.execute('SELECT status,error FROM task_runs WHERE id=?', (ctx['run_id'],)).fetchone()
    assert run['status'] == 'crashed'
    assert 'causal final output' in run['error']
    assert 'fixture worker_receipt_timeout' not in run['error']


@pytest.mark.parametrize('damage', ['none', 'missing_identity', 'foreign_claim', 'foreign_pid'])
def test_host_refusal_requires_attributable_modern_receipt(board, damage):
    from hermes_cli import kanban_host_refusal as host
    from hermes_cli.kanban_owner_unavailable import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
    conn, ctx = board
    row = fixtures._failed_child(conn, ctx)
    result = json.loads(row['payload'])
    result.update(exit_code=KANBAN_OWNER_UNAVAILABLE_EXIT_CODE,
                  host_refusal={'kind': host.REGISTRY_EMPTY, 'phase': 'startup'})
    if damage == 'missing_identity':
        result.pop('bound_event_identity')
    if damage == 'foreign_claim':
        result['claim_lock'] = 'foreign:claim'
    if damage == 'foreign_pid':
        result['pid'] += 1
    with kb.write_txn(conn):
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(result), row['id']))
    assert bool(host.proven(conn, ctx['task_id'], ctx['run_id'], ctx['claim_lock'])) is (damage == 'none')


def test_output_and_exit_follow_causal_result(board):
    conn, ctx, first = _history(board, {'exit_code': 7, 'last_output': 'causal final output'})
    assert logs.managed_worker_output(conn, ctx['task_id'], ctx['run_id'], first['pid'], ctx['claim_lock']) == 'causal final output'
    assert owner.managed_run_exit_code(conn, ctx['task_id'], first['pid'], ctx['claim_lock']) == 7


def test_quota_metadata_follows_causal_result(board):
    conn, ctx, _ = _history(board, {'exit_code': kb.KANBAN_RATE_LIMIT_EXIT_CODE,
                                  'failure_reason': 'rate_limit', 'failure_resets_at': 1900000000,
                                  'route_provider': 'example-provider'})
    assert quota.receipt_deadline(conn, ctx['task_id'], ctx['run_id']) == 1900000000
    assert quota.receipt_route(conn, ctx['task_id'], ctx['run_id']) == 'example-provider'


@pytest.mark.parametrize('output,expected', [(STORAGE, True), ('ordinary failure', False)])
def test_storage_classification_follows_causal_result(board, output, expected):
    conn, ctx, _ = _history(board, {'exit_code': 1, 'last_output': output})
    with kb.write_txn(conn):
        row = conn.execute("SELECT id,payload FROM task_events WHERE kind='worker_result' ORDER BY id DESC LIMIT 1").fetchone()
        old = json.loads(row['payload'])
        old['last_output'] = 'ordinary failure' if expected else STORAGE
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(old), row['id']))
    assert bool(storage.proven(conn, ctx['task_id'], ctx['run_id'], ctx['claim_lock'])) is expected


def test_ambiguous_modern_history_supplies_no_classifier_evidence(board):
    conn, ctx, first = _history(board, {'last_output': STORAGE}, ambiguous=True)
    assert logs.managed_worker_output(conn, ctx['task_id'], ctx['run_id'], first['pid'], ctx['claim_lock']) == ''
    assert owner.managed_run_exit_code(conn, ctx['task_id'], first['pid'], ctx['claim_lock']) is None
    assert storage.proven(conn, ctx['task_id'], ctx['run_id'], ctx['claim_lock']) is None
    assert quota.receipt_deadline(conn, ctx['task_id'], ctx['run_id']) is None
