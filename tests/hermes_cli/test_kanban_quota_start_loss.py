"""Quota startup receipts survive an owner's subsequent unknown/loss echo."""
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.session_kanban import record_worker_loss
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli import kanban_worker_client as client, kanban_worker_log as worker_log
from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.kanban_quota_episode import hold_until
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures
from tools.environments.local import build_subprocess_env

board = fixtures.board


def quota_child(conn, context):
    reset = time.time() + 3600
    source = fixtures._CHILD.replace(
        "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')",
        "from agent.model_routing_unavailable import SubscriptionQuotaUnavailable\n"
        "from gateway.session_kanban import start_failure_line\n"
        "failure = SubscriptionQuotaUnavailable('anthropic', 'claude-opus-5-5', " + repr(reset) + ")\n"
        "_record_worker_result(context, 75, start_failure_line(failure), failure)").replace(
            'raise SystemExit(1)', 'raise SystemExit(75)')
    proc = subprocess.Popen([sys.executable, '-c', source, json.dumps(context), 'unit'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', env=build_subprocess_env(),
                            cwd=Path(__file__).resolve().parents[2])
    try:
        dispatch._set_worker_pid(conn, context['task_id'], proc.pid)
        output = proc.communicate('x', timeout=30)
        assert proc.returncode == 75, output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    record_worker_loss(SimpleNamespace(kanban_json=json.dumps(context)),
                       'managed_worker_failed', pid=proc.pid, bound=True)
    with pytest.raises(GatewayClientError, match='execution unknown') as refusal:
        client._exit_code({'status': 'unknown'}, context, context['db'])
    loss = conn.execute("SELECT payload FROM task_events WHERE kind='worker_lost'").fetchone()
    assert not json.loads(loss['payload']).get('loss_receipt')
    with kb.write_txn(conn):
        assert not worker_log.record_submitter_result(conn, context, proc.pid, 1, str(refusal.value))
    from hermes_cli.kanban_failed_result_identity import modern_receipt
    modern, selected = modern_receipt(conn, context['task_id'], context['run_id'], context['claim_lock'])
    assert modern and selected[1]['exit_code'] == 75
    return reset


def test_quota_unknown_owner_releases_in_one_tick(board):
    conn, context = board
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET consecutive_failures=1, max_retries=2')
    reset = quota_child(conn, context)
    dispatch.detect_crashed_workers(conn)
    task = kb.get_task(conn, context['task_id'])
    assert task.status != 'running'
    assert task.claim_lock is None
    assert task.consecutive_failures == 1
    assert task.status != 'triage'
    assert hold_until(conn, task.id) >= reset
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='failure_finalized'").fetchone()[0] == 0


@pytest.mark.parametrize('change', ['none', 'zero', 'bool', 'continuable', 'owner_restart', 'same_pid_rebound'])
def test_loss_witness_preserves_only_final_interpreter_failure(board, change):
    from hermes_cli.kanban_lost_result import loss_identity_fields
    from tests.hermes_cli import test_kanban_owner_restart_failed_receipt as restart
    conn, context = board
    fixtures._failed_child(conn, context)
    result = conn.execute("SELECT * FROM task_events WHERE kind='worker_result'").fetchone()
    payload = json.loads(result['payload'])
    pid = payload['pid']
    if change == 'owner_restart':
        restart.book(context)
    with kb.write_txn(conn):
        if change == 'same_pid_rebound':
            binding = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='worker_bound'").fetchone()[0])
            binding['started_at'] = 'another-incarnation|123'
            kb._append_event(conn, context['task_id'], 'worker_bound', binding, run_id=context['run_id'])
        elif change not in ('none', 'owner_restart'):
            key, value = {'zero': ('exit_code', 0), 'bool': ('exit_code', True),
                          'continuable': ('continuable', True)}[change]
            payload[key] = value
            conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), result['id']))
        fields = loss_identity_fields(conn, context, pid, True)
    assert bool(fields.get('loss_receipt')) == (change != 'none')
