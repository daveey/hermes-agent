"""Real rc0 child calls the registered tool; real gateway writer and sweep settle legally."""
import json
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_failed_result_reconcile as support
from tools.environments.local import build_subprocess_env

board = support.board

CHILD = '''
import json, os, sys
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect_closing
from gateway.session_context import scoped_current_session_id
from gateway.session_kanban import _record_worker_result
from tools import kanban_tools
from tools.registry import registry
context = json.loads(sys.argv[1])
sys.stdin.read(1)
os.environ.update(HERMES_KANBAN_TASK=context['task_id'], HERMES_KANBAN_DB=context['db'],
                  HERMES_KANBAN_RUN_ID=str(context['run_id']), HERMES_KANBAN_CLAIM_LOCK=context['claim_lock'],
                  HERMES_SESSION_ID='fixture-retain-process')
with scoped_current_session_id('fixture-retain-process'):
    with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'owner_admitted', {'session_id':'fixture-retain-process'}, run_id=context['run_id'])
        kb._append_event(conn, context['task_id'], 'worker_bound', {'pid':os.getpid(),
            'started_at':dispatch._process_fingerprint(os.getpid()),'claim_lock':context['claim_lock']}, run_id=context['run_id'])
    result = json.loads(registry.get_entry('kanban_retain').handler(dict(summary='Incomplete stopped scope',
        reason='scope_stopped', next_step='Administrative owner must resolve scope',
        release_condition='Supported explicit authority', evidence=['synthetic rc0 proof'], stop_on='owner_instruction')))
    assert result.get('terminal_for_run') is True, result
    _record_worker_result(context, 0, 'retained without acceptance completion')
    print(json.dumps(result))
'''


def test_actual_registered_tool_rc0_is_not_protocol_violation(board):
    conn, context = board
    proc = subprocess.Popen([sys.executable, '-c', CHILD, json.dumps(context)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                            cwd=str(Path(__file__).resolve().parents[2]), env=build_subprocess_env())
    try:
        dispatch._set_worker_pid(conn, context['task_id'], proc.pid)
        out, err = proc.communicate('x', timeout=40)
        assert proc.returncode == 0, (out, err)
        assert json.loads(out.strip().splitlines()[-1])['terminal_for_run'] is True
        dispatch.detect_crashed_workers(conn)
        task = kb.get_task(conn, context['task_id'])
        assert task.status == 'triage' and task.consecutive_failures == 0 and task.current_run_id is None
        run = conn.execute('SELECT outcome,ended_at FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
        assert run['outcome'] == 'retained' and run['ended_at'] is not None
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='protocol_violation'", (task.id,)).fetchone()
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN ('review_requested','released')", (task.id,)).fetchone()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
