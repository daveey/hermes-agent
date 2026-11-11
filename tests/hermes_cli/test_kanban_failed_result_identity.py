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
