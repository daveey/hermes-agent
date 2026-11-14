"""Loss writer contracts before emission, independent of consumer masking (#2857)."""
import json
from types import SimpleNamespace

import pytest

from gateway.session_kanban import record_worker_loss
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failed_result as proof
from hermes_cli import kanban_lost_result as loss
from hermes_cli.kanban_failed_result_identity import result_identity_fields
from tests.hermes_cli import test_kanban_rebound_worker_loss as rebound

board = rebound.board


def damage_before_loss(conn, context, damage):
    bound = rebound.lost_after_restart(conn, context, emit_loss=False)
    pid = bound['pid']
    if damage == 'wrong_pid':
        return pid + 100000000
    statements = {
        'missing_binding': ("DELETE FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_bound'",
                            (context['task_id'], context['run_id'])),
        'missing_birth': ('UPDATE tasks SET worker_started_at=NULL WHERE id=?', (context['task_id'],)),
    }
    with kb.write_txn(conn):
        if damage in statements:
            conn.execute(*statements[damage])
        else:
            assert damage == 'wrong_claim'
            row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND run_id=? "
                               "AND kind='worker_bound' ORDER BY id DESC LIMIT 1",
                               (context['task_id'], context['run_id'])).fetchone()
            payload = json.loads(row['payload'])
            payload['claim_lock'] = 'foreign:1'
            conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
    return pid


def emit_loss(conn, context, pid):
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='worker_lost'").fetchone()[0] == 0
    record_worker_loss(SimpleNamespace(kanban_json=json.dumps(context)), 'EOFError', pid=pid, bound=True)
    events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_lost'",
                          (context['task_id'], context['run_id'])).fetchall()
    assert len(events) == 1, 'unverified loss must still retain its diagnostic event'
    return json.loads(events[0]['payload'])


@pytest.mark.parametrize('damage', ['missing_binding', 'missing_birth', 'wrong_pid', 'wrong_claim'])
def test_unverified_loss_preserves_diagnostic_without_attesting_receipt(board, damage):
    conn, context = board
    pid = damage_before_loss(conn, context, damage)
    payload = emit_loss(conn, context, pid)
    assert payload['reason'] == 'EOFError'
    assert payload['pid'] == pid
    assert 'loss_receipt' not in payload
    assert 'death_proof_version' not in payload


@pytest.mark.parametrize('damage', ['missing_birth', 'wrong_pid', 'wrong_claim'])
@pytest.mark.parametrize('observer', [False, True])
def test_consumer_holds_even_if_writer_attests_invalid_loss(board, monkeypatch, damage, observer):
    conn, context = board
    pid = damage_before_loss(conn, context, damage)

    def unchecked_identity(conn, context, pid, bound):
        # Inject a faulty producer at its seam, not a consumer bypass. These
        # controls explain masking; they do not make the writer guards redundant.
        return {'death_proof_version': 1, 'loss_receipt': True, 'exit_code': None,
                **result_identity_fields(conn, context)}

    monkeypatch.setattr(loss, 'loss_identity_fields', unchecked_identity)
    assert emit_loss(conn, context, pid)['loss_receipt'] is True
    assert proof.failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']) is None
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    task = kb.get_task(conn, context['task_id'])
    run = conn.execute('SELECT status, ended_at FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert task.status == 'running'
    assert task.claim_lock == context['claim_lock']
    assert task.current_run_id == context['run_id']
    assert task.consecutive_failures == 0
    assert run['status'] == 'running' and run['ended_at'] is None
