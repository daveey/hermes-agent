"""Independent refusal evidence fences; ordinary death proof alone is insufficient."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_refusal_proof as refusal
from tests.hermes_cli import test_kanban_refusal_stop as fixtures

board = fixtures.board


@pytest.mark.parametrize('corruption', ['retryable', 'claim', 'legacy_order', 'modern_identity'])
def test_select_refusal_rejects_individually_wrong_provenance(board, monkeypatch, corruption):
    from hermes_cli.kanban_failed_result import failed_result_proof
    conn, context = board
    result = fixtures.refused_child(conn, context, monkeypatch)
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (context['task_id'],)).fetchone()
    proof = failed_result_proof(conn, row['id'], row['current_run_id'], row['claim_lock'])
    assert proof is not None
    assert refusal.select_refusal(conn, row, proof) is not None
    payload = json.loads(result['payload'])
    if corruption == 'retryable':
        payload['failure_retryable'] = True
    elif corruption == 'claim':
        payload['claim_lock'] = 'foreign:claim'
    elif corruption == 'modern_identity':
        payload['bound_event_identity'] = 'foreign:event'
    else:
        payload.pop('death_proof_version', None)
        payload.pop('bound_event_identity', None)
    with kb.write_txn(conn):
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), result['id']))
        if corruption == 'legacy_order':
            bound = conn.execute("SELECT * FROM task_events WHERE run_id=? AND kind='worker_bound'",
                                 (context['run_id'],)).fetchone()
            assert bound is not None
            conn.execute('DELETE FROM task_events WHERE id=?', (bound['id'],))
            kb._append_event(conn, row['id'], 'worker_bound', json.loads(bound['payload']),
                             run_id=context['run_id'])
    assert refusal.select_refusal(conn, row, proof) is None


def test_release_surfaces_identify_safety_not_budget_recovery(board, monkeypatch):
    from hermes_cli import kanban_spend_promote as promote
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_spend_decision as decision
    conn, context = board
    fixtures.refused_child(conn, context, monkeypatch)
    dispatch.detect_crashed_workers(conn)
    stop = decision.pending_episode(conn, context['task_id'])
    assert stop is not None
    reason = promote.scope_refusal(conn, context['task_id'], 'fixture-supervisor',
                                   stop.get('uid') or str(stop['id']), 1)
    assert reason and 'safety restriction' in reason and 'not clearance' in reason
    reason = decision.unblock_refusal(conn, context['task_id'])
    assert reason and 'safety restriction' in reason and 'no supported execution release' in reason
    assert 'manually promote' not in reason
