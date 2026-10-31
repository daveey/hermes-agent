"""Quota receipts retain the reset through worker startup and admission."""
import json
from types import SimpleNamespace

import pytest

from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from gateway import session_kanban
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect_closing
from tests.gateway.test_kanban_result_exit import _bound_claim


@pytest.mark.parametrize('startup', [False, True])
def test_quota_receipt_gates_admission_until_reset(tmp_path, monkeypatch, startup):
    now = 1_900_000_000
    reset = now + 7200
    monkeypatch.setattr(kb.time, 'time', lambda: now)
    path, context = _bound_claim(tmp_path, monkeypatch)
    frame = {'policy': {'kanban_json': json.dumps(context)}}
    if startup:
        session_kanban.record_start_failure(frame, SubscriptionQuotaUnavailable('anthropic', 'exact-model', reset))
    else:
        result = {'failed': True, 'completed': False, 'failure_reason': 'rate_limit',
                  'failure_retryable': True, 'failure_resets_at': reset}
        monkeypatch.setattr(session_kanban, '_run_task_turns', lambda *_: result)
        session_kanban.run_worker_turns(SimpleNamespace(), frame, [])
    with connect_closing(path) as conn:
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result' ORDER BY id DESC LIMIT 1",
                           (context['task_id'],)).fetchone()
        receipt = json.loads(row[0])
        assert receipt['exit_code'] == 75
        assert receipt['failure_reason'] == 'rate_limit'
        assert receipt['failure_resets_at'] == reset
        conn.execute("UPDATE task_runs SET outcome='rate_limited',status='rate_limited',ended_at=? WHERE id=?",
                     (now, context['run_id']))
        conn.execute("UPDATE tasks SET status='ready',current_run_id=NULL,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL WHERE id=?",
                     (context['task_id'],))
        conn.commit()
        monkeypatch.setattr(kb.time, 'time', lambda: now + 600)
        assert dispatch.check_respawn_guard(conn, context['task_id']) == 'subscription_quota_reset'
        assert dispatch.guard_hold_cap_seconds('subscription_quota_reset') is None
        monkeypatch.setattr(kb.time, 'time', lambda: reset + 1)
        assert dispatch.check_respawn_guard(conn, context['task_id']) is None
