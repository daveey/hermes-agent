"""Causal result selection must survive local integer reassignment."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_failed_result import failed_result_proof
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


def test_ttl_reclaim_reserves_modern_failed_run_for_strict_finalizer(board, monkeypatch):
    from hermes_cli import kanban_owner_recovery as owner
    conn, context = board
    fixtures._failed_child(conn, context)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET claim_expires=1, last_heartbeat_at=1 WHERE id=?',
                     (context['task_id'],))
    monkeypatch.setattr(owner, 'owner_reclaim_paused', lambda *args: False)
    assert kb.release_stale_claims(conn) == 0
    assert conn.execute('SELECT status FROM task_runs WHERE id=?',
                        (context['run_id'],)).fetchone()[0] == 'running'


@pytest.mark.parametrize('prior', ['success', 'continuable'])
def test_real_writer_failure_after_prior_result_is_reconcilable(board, monkeypatch, prior):
    conn, context = board
    extra = ", extra={'continuable': True}" if prior == 'continuable' else ''
    earlier = f"_record_worker_result(context, 0, 'earlier turn'{extra})\n"
    monkeypatch.setattr(fixtures, '_CHILD', fixtures._CHILD.replace(
        "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')",
        earlier + "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')"))
    # Helper checks the first receipt, so fetch the production child directly
    # through its existing harness but assert the multi-receipt outcome here.
    import subprocess
    import sys
    from pathlib import Path
    from tools.environments.local import build_subprocess_env
    from hermes_cli import kanban_db_dispatch as dispatch
    proc = subprocess.Popen([sys.executable, '-c', fixtures._CHILD, json.dumps(context), 'unit'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', env=build_subprocess_env(),
                            cwd=str(Path(__file__).resolve().parents[2]))
    try:
        dispatch._set_worker_pid(conn, context['task_id'], proc.pid)
        out, err = proc.communicate('x', timeout=30)
        assert proc.returncode == 1, (out, err)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    dispatch.detect_crashed_workers(conn, failed_results_only=True)
    assert conn.execute('SELECT status FROM task_runs WHERE id=?',
                        (context['run_id'],)).fetchone()[0] == 'crashed'


@pytest.mark.parametrize('prior_live', [False, True])
@pytest.mark.parametrize('observer_path', [False, True])
def test_rebound_failure_preserves_every_bound_process_fence(board, monkeypatch, prior_live, observer_path):
    import os
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, context = board
    if prior_live:
        with kb.write_txn(conn):
            kb._append_event(conn, context['task_id'], 'worker_bound',
                             {'pid': os.getpid(), 'started_at': dispatch._process_fingerprint(os.getpid()),
                              'claim_lock': context['claim_lock']}, run_id=context['run_id'])
    else:
        fixtures._failed_child(conn, context)
    fixtures._failed_child(conn, context)
    # Reverse binding order as a replica may do, without changing its identity.
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET id=id+2000 WHERE id=(SELECT MIN(id) FROM task_events WHERE kind='worker_bound')")
    dispatch.detect_crashed_workers(conn, failed_results_only=observer_path)
    state = conn.execute('SELECT status FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()[0]
    assert state == ('running' if prior_live else 'crashed')


@pytest.mark.parametrize('sequence', [None, True, 0, -1, '1'])
def test_snapshot_cannot_validate_invalid_selected_sequence(board, sequence):
    conn, context = board
    row = fixtures._failed_child(conn, context)
    payload = json.loads(row['payload'])
    payload['result_sequence'] = sequence
    with kb.write_txn(conn):
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
    assert failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']) is None


def test_unseen_legacy_result_cannot_be_hidden_by_history_snapshot(board):
    from hermes_cli.kanban_failed_result_identity import result_identity_fields
    conn, context = board
    row = fixtures._failed_child(conn, context)
    payload = json.loads(row['payload'])
    with kb.write_txn(conn):
        # A later valid marked result observes the first result, not the final
        # legacy result appended afterwards. No observer may guess its order.
        kb._append_event(conn, context['task_id'], 'worker_result',
                         {**payload, **result_identity_fields(conn, context)}, run_id=context['run_id'])
        kb._append_event(conn, context['task_id'], 'worker_result',
                         {'exit_code': 0, 'pid': payload['pid'], 'claim_lock': context['claim_lock']},
                         run_id=context['run_id'])
    assert failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']) is None


def test_result_sequence_not_import_order_selects_latest(board):
    conn, context = board
    result = fixtures._failed_child(conn, context)
    first = json.loads(result['payload'])
    second = {**first, 'result_sequence': 2, 'last_output': 'later failure', 'exit_code': 7}
    with kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'worker_result', second, run_id=context['run_id'])
        conn.execute('UPDATE task_events SET id=? WHERE id=?', (result['id'] + 1000, result['id']))
    proof = failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock'])
    assert proof is not None
    assert proof['result']['exit_code'] == 7
    assert proof['result']['last_output'] == 'later failure'


@pytest.mark.parametrize('damage', ['duplicate_sequence', 'missing_sequence', 'bool_sequence',
                                   'missing_identity', 'rebound', 'malformed_result'])
def test_ambiguous_modern_history_fails_closed(board, damage):
    conn, context = board
    result = fixtures._failed_child(conn, context)
    payload = json.loads(result['payload'])
    payload['result_sequence'] = 2
    replacements = {'duplicate_sequence': {'result_sequence': 1},
                    'bool_sequence': {'result_sequence': True}}
    payload.update(replacements.get(damage, {}))
    missing = {'missing_sequence': 'result_sequence', 'missing_identity': 'bound_event_identity'}
    if damage in missing:
        payload.pop(missing[damage])
    with kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'worker_result', payload, run_id=context['run_id'])
        if damage == 'malformed_result':
            conn.execute("UPDATE task_events SET payload='[]' WHERE id=(SELECT MAX(id) FROM task_events)")
        if damage == 'rebound':
            kb._append_event(conn, context['task_id'], 'worker_bound',
                             {'pid': 1, 'started_at': 'other|1', 'claim_lock': context['claim_lock']},
                             run_id=context['run_id'])
    assert failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']) is None
