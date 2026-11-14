"""A continued worker lost before its result must not retain a modern claim."""
import json
from types import SimpleNamespace

import pytest

from gateway.session_kanban import record_worker_loss
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failed_result as proof
from tests.hermes_cli import test_kanban_owner_restart_failed_receipt as restart

board = restart.board


def lost_after_restart(conn, context, *, emit_loss=True):
    restart._failed_child(conn, context)
    restart.book(context, True)
    import subprocess
    import sys
    from pathlib import Path
    from tools.environments.local import build_subprocess_env
    child = restart._CONTINUED_CHILD.replace('_record_worker_result(context, code, output)', 'pass')
    done = subprocess.run([sys.executable, '-c', child, json.dumps(context), '1', ''],
                          capture_output=True, text=True, encoding='utf-8', timeout=60, check=False,
                          env=build_subprocess_env(), cwd=str(Path(__file__).resolve().parents[2]))
    assert done.returncode == 0, done.stderr
    bound = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='worker_bound' ORDER BY id DESC LIMIT 1").fetchone()[0])
    if emit_loss:
        record_worker_loss(SimpleNamespace(kanban_json=json.dumps(context)), 'EOFError', pid=bound['pid'], bound=True)
    return bound


@pytest.mark.parametrize('observer', [False, True])
def test_rebound_loss_closes_once_without_fabricating_exit_status(board, observer):
    conn, context = board
    lost_after_restart(conn, context)
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    task = kb.get_task(conn, context['task_id'])
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert task.status == 'ready'
    assert run['outcome'] == 'crashed' and run['ended_at'] is not None
    assert task.consecutive_failures == 1
    assert 'EOFError' in run['error']
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    assert kb.get_task(conn, context['task_id']).consecutive_failures == 1


@pytest.mark.parametrize('damage', ['live', 'wrong_pid', 'wrong_claim', 'unbound', 'later_binding', 'later_result'])
def test_loss_cannot_close_unproven_or_superseded_attempt(board, monkeypatch, damage):
    conn, context = board
    bound = lost_after_restart(conn, context)
    if damage == 'live':
        monkeypatch.setattr(proof, 'process_gone', lambda *a: False)
    else:
        with kb.write_txn(conn):
            event = conn.execute("SELECT * FROM task_events WHERE kind='worker_lost'").fetchone()
            payload = json.loads(event['payload'])
            if damage.startswith('later_'):
                kind = 'worker_bound' if damage == 'later_binding' else 'worker_result'
                kb._append_event(conn, context['task_id'], kind, bound, run_id=context['run_id'])
            else:
                key, value = {'wrong_pid': ('pid', 1), 'wrong_claim': ('claim_lock', 'foreign:1'),
                              'unbound': ('bound', False)}[damage]
                payload[key] = value
                conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event['id']))
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, context['task_id']).status == 'running'
