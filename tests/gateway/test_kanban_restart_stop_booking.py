"""Restart Stop disposition reaches the real worker client and dead-worker sweep."""
import json
import time
from types import SimpleNamespace

import pytest

from gateway.session_kanban import worker_result
from gateway.session_kanban_restart import record_restart_interruption
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.kanban_worker_client import _exit_code


@pytest.mark.parametrize('bootstrapped', [True, False])
def test_restart_stop_requeues_uncounted(tmp_path, monkeypatch, bootstrapped):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.setattr(kb, '_pid_alive', lambda _: False)
    monkeypatch.setattr(kbd, '_worker_final_output', lambda *a, **kw: '')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='restart card', assignee='default', max_retries=1)
        lock = kb._claimer_id().split(':', 1)[0] + ':restart-test'
        kb.claim_task(conn, tid, claimer=lock)
        task = kb.get_task(conn, tid)
        params = {'task_id': tid, 'run_id': task.current_run_id, 'claim_lock': lock}
        context = dict(params, db=str(tmp_path / 'kanban.db'))
        policy = SimpleNamespace(kanban_json=json.dumps(context))
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET worker_pid=?,started_at=? WHERE id=?',
                         (79101, int(time.time()) - 600, tid))
            if bootstrapped:
                kb._append_event(conn, tid, 'worker_result', {'claim_lock': lock, 'exit_code': 130},
                                 run_id=task.current_run_id)
        record_restart_interruption(policy)
        code = _exit_code({'status': 'terminal', 'outcome': 'interrupted'}, params, context['db'])
        assert code == 76
        monkeypatch.setattr(kbd, '_recent_worker_exits', {79101: ((code & 0xff) << 8, time.time())})
        assert kbd.detect_crashed_workers(conn) == []
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures, task.block_kind) == ('ready', 0, None)
        assert conn.execute('SELECT outcome FROM task_runs WHERE id=?', (params['run_id'],)).fetchone()[0] == 'reclaimed'
        kinds = [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=?', (tid,))]
        assert kinds.count('owner_restart_lost') == 1
        assert 'crashed' not in kinds and 'gave_up' not in kinds


@pytest.mark.parametrize('mismatch', ['task_lock', 'run_lock', 'run_id', 'done'])
def test_old_attempt_cannot_overwrite_result(tmp_path, monkeypatch, mismatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='fenced card', assignee='default')
        kb.claim_task(conn, tid, claimer='host:1')
        task = kb.get_task(conn, tid)
        context = {'task_id': tid, 'run_id': task.current_run_id, 'claim_lock': 'host:1',
                   'db': str(tmp_path / 'kanban.db')}
        with kb.write_txn(conn):
            if mismatch == 'task_lock':
                conn.execute('UPDATE tasks SET claim_lock=? WHERE id=?', ('other', tid))
            elif mismatch == 'run_lock':
                conn.execute('UPDATE task_runs SET claim_lock=? WHERE id=?', ('other', task.current_run_id))
            elif mismatch == 'run_id':
                context['run_id'] += 1
            else:
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        record_restart_interruption(SimpleNamespace(kanban_json=json.dumps(context)))
        assert worker_result(context['db'], context) == {}
