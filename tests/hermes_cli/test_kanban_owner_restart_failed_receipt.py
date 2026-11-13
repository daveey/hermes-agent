"""Native restart bookings supersede child failures without weakening death fences."""
import json
import os
from types import SimpleNamespace

import pytest

from gateway.session_kanban_restart import record_restart_interruption
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failed_result as proof
from hermes_cli import kanban_owner_recovery as owner
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board
_failed_child = fixtures._failed_child


def book(context, checkpoint=False):
    record_restart_interruption(SimpleNamespace(kanban_json=json.dumps(context)),
                                {'session_id': 'fixture'} if checkpoint else None)


def latest(conn):
    return conn.execute("SELECT * FROM task_events WHERE kind='worker_result' ORDER BY id DESC LIMIT 1").fetchone()


@pytest.mark.parametrize('observer', [False, True])
def test_native_owner_supersedes_failed_child(board, monkeypatch, observer):
    conn, context = board
    _failed_child(conn, context)
    book(context)
    result = latest(conn)
    payload = json.loads(result['payload'])
    assert payload['death_proof_version'] == 1
    assert payload['result_role'] == 'owner_restart'
    assert owner.managed_run_exit_code(conn, context['task_id'], payload['pid'], context['claim_lock']) == 76
    monkeypatch.setattr(owner, 'apply_attempt_note', lambda *a: pytest.fail('state.db fallback'))
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert run['status'] != 'running'
    assert kb.get_task(conn, context['task_id']).consecutive_failures == 0
    event = conn.execute("SELECT payload FROM task_events WHERE kind='owner_restart_lost'").fetchone()
    receipt = json.loads(event['payload'])
    assert receipt['source_result_event_id'] == result['id']
    assert receipt['result_recorded_at'] == result['created_at']
    assert receipt['reconciled_at'] >= result['created_at']


@pytest.mark.parametrize('observer', [False, True])
def test_owner_only_witnessed_launch(board, monkeypatch, observer):
    conn, context = board
    _failed_child(conn, context)
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_events WHERE kind IN ('worker_result','worker_bound')")
    book(context)
    assert json.loads(latest(conn)['payload'])['death_proof_version'] == 1
    monkeypatch.setattr(owner, 'apply_attempt_note', lambda *a: pytest.fail('state.db fallback'))
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    assert kb.get_task(conn, context['task_id']).status != 'running'


def test_continuable_observer_holds_dispatcher_closes(board):
    conn, context = board
    _failed_child(conn, context)
    book(context, True)
    assert proof.failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']) is None
    dispatch.detect_crashed_workers(conn, failed_results_only=True)
    assert kb.get_task(conn, context['task_id']).status == 'running'
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, context['task_id']).status != 'running'
    assert kb.get_task(conn, context['task_id']).consecutive_failures == 0


@pytest.mark.parametrize('damage', ['role', 'code', 'code_float', 'owner_flag', 'outer_identity', 'task_birth', 'later_unmarked'])
@pytest.mark.parametrize('observer', [False, True])
def test_damaged_owner_receipt_holds(board, monkeypatch, damage, observer):
    conn, context = board
    _failed_child(conn, context)
    book(context)
    row = latest(conn)
    payload = json.loads(row['payload'])
    with kb.write_txn(conn):
        if damage == 'task_birth':
            conn.execute("UPDATE tasks SET worker_started_at='different|1'")
        elif damage == 'later_unmarked':
            kb._append_event(conn, context['task_id'], 'worker_result',
                             {'pid': payload['pid'], 'claim_lock': context['claim_lock'], 'exit_code': 76},
                             run_id=context['run_id'])
        else:
            key, value = {'role': ('result_role', 'other'), 'code': ('exit_code', 1),
                          'code_float': ('exit_code', 76.0), 'owner_flag': ('owner_restart', False),
                          'outer_identity': ('outer_event_identity', {'pid': payload['pid']})}[damage]
            payload[key] = value
            conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
    monkeypatch.setattr(dispatch, '_worker_alive', lambda *a: False)
    monkeypatch.setattr(owner, 'apply_attempt_note', lambda *a: pytest.fail('state.db fallback'))
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    assert kb.get_task(conn, context['task_id']).status == 'running'


@pytest.mark.parametrize('checkpoint', [False, True])
def test_distinct_live_outer_stays_resumable(board, checkpoint):
    conn, context = board
    _failed_child(conn, context, outer_pid=os.getpid())
    book(context, checkpoint)
    payload = json.loads(latest(conn)['payload'])
    assert payload['pid'] == os.getpid()
    assert payload['outer_event_identity']['pid'] == os.getpid()
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, context['task_id']).status == 'running'


def test_unwitnessed_owner_remains_legacy(board):
    conn, context = board
    book(context)
    assert 'death_proof_version' not in json.loads(latest(conn)['payload'])


@pytest.mark.parametrize('observer', [False, True])
@pytest.mark.parametrize('checkpoint', [False, True])
def test_distinct_outer_exit_and_all_historical_bindings(board, observer, checkpoint):
    import subprocess
    import sys
    from tools.environments.local import build_subprocess_env
    conn, context = board
    outer = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, env=build_subprocess_env())
    try:
        _failed_child(conn, context, outer_pid=outer.pid)
        with kb.write_txn(conn):
            kb._append_event(conn, context['task_id'], 'worker_bound',
                             {'pid': os.getpid(), 'started_at': dispatch._process_fingerprint(os.getpid()),
                              'claim_lock': context['claim_lock']}, run_id=context['run_id'])
        book(context, checkpoint)
        assert json.loads(latest(conn)['payload'])['pid'] == outer.pid
        outer.communicate(timeout=5)
        # The historical live binding remains a veto even after the outer exits.
        dispatch.detect_crashed_workers(conn, failed_results_only=observer)
        assert kb.get_task(conn, context['task_id']).status == 'running'
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_events WHERE kind='worker_bound' AND json_extract(payload,'$.pid')=?",
                         (os.getpid(),))
        # Removing a predecessor is itself damage; a fresh booking witnesses the complete history.
        book(context, checkpoint)
        dispatch.detect_crashed_workers(conn, failed_results_only=observer)
        held = observer and checkpoint
        assert (kb.get_task(conn, context['task_id']).status == 'running') == held
    finally:
        if outer.poll() is None:
            outer.communicate(timeout=5)


def test_owner_snapshot_survives_replica_event_ids(board):
    conn, context = board
    _failed_child(conn, context)
    book(context)
    with kb.write_txn(conn):
        conn.execute('UPDATE task_events SET id=id+1000')
    assert proof.failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock'])


_CONTINUED_CHILD = '''
import json, os, sys
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_connect import connect_closing
from gateway.session_kanban import _record_worker_result
import hermes_cli.kanban_failed_result_observer as observer
observer.arm = lambda context: None
context, code, output = json.loads(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
    kb._append_event(conn, context['task_id'], 'worker_bound',
        {'pid': os.getpid(), 'started_at': kbd._process_fingerprint(os.getpid()),
         'claim_lock': context['claim_lock']}, run_id=context['run_id'])
_record_worker_result(context, code, output)
'''


def _continued_turn(context, code, output):
    """The next owner's interpreter for the SAME run: binds, ends its continued turn with ``code``, exits."""
    import subprocess
    import sys
    from pathlib import Path
    from tools.environments.local import build_subprocess_env
    done = subprocess.run([sys.executable, '-c', _CONTINUED_CHILD, json.dumps(context), str(code), output],
                          capture_output=True, text=True, timeout=60, env=build_subprocess_env(), check=False,
                          cwd=str(Path(__file__).resolve().parents[2]))
    assert done.returncode == 0, (done.stdout, done.stderr)


def test_a_continued_run_whose_worker_exits_cleanly_is_a_protocol_violation(board):
    """t_01a1d563324d7e8e run 11569 (worker-6, 11-13): the owner's continuable restart booking makes the
    run's evidence modern; the continued interpreter's rc=0 result carried no modern identity, so no
    result was attributable, ``reclaim_proof`` held, and the card sat ``running`` with every pid dead."""
    conn, context = board
    _failed_child(conn, context)          # the first owner's interpreter, stopped by the restart
    book(context, True)                   # the stopping owner's continuable checkpoint (exit 76)
    last = "I stopped retrying because I kept running kanban_link 8 times. Tell me how you'd like to proceed"
    _continued_turn(context, 0, last)     # the next owner continues the turn; it ends without a terminal call
    dispatch.detect_crashed_workers(conn)  # one pass, every pid of the run already dead
    task = kb.get_task(conn, context['task_id'])
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert task is not None and task.last_failure_error is not None
    assert task.status == 'ready', dict(run)
    assert run['outcome'] == 'crashed' and run['ended_at'] is not None
    assert json.loads(run['metadata'])['protocol_violation'] is True
    assert run['error'].startswith(dispatch._PROTOCOL_VIOLATION_ERROR)
    assert last[:60] in run['error'] and last[:60] in task.last_failure_error
    assert task.last_failure_error.startswith(dispatch._PROTOCOL_VIOLATION_ERROR)
    kinds = [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE run_id=?', (context['run_id'],))]
    assert kinds.count('protocol_violation') == 1


def test_a_continued_run_whose_worker_fails_is_still_a_crash(board):
    """The same continued shape with a failing worker keeps the strict failed-result booking."""
    conn, context = board
    _failed_child(conn, context)
    book(context, True)
    _continued_turn(context, 1, 'continued turn failed')
    dispatch.detect_crashed_workers(conn)
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert run['outcome'] == 'crashed' and 'protocol_violation' not in json.loads(run['metadata'])
    assert 'continued turn failed' in run['error']


def test_a_live_continued_interpreter_with_a_clean_result_still_holds(board, monkeypatch):
    """A clean receipt is not a death proof: a still-live bound interpreter keeps the run."""
    conn, context = board
    _failed_child(conn, context)
    book(context, True)
    _continued_turn(context, 0, 'done talking')
    real = proof.process_gone
    bound = [json.loads(r[0])['pid'] for r in conn.execute(
        "SELECT payload FROM task_events WHERE kind='worker_bound' ORDER BY id")][-1]
    monkeypatch.setattr(proof, 'process_gone', lambda pid, birth: pid != bound and real(pid, birth))
    dispatch.detect_crashed_workers(conn)
    task = kb.get_task(conn, context['task_id'])
    assert task is not None and task.status == 'running'


def test_child_can_supersede_owner_after_continuation_rebind(board):
    from hermes_cli.kanban_failed_result_identity import result_identity_fields
    conn, context = board
    _failed_child(conn, context)
    book(context, True)
    bound = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='worker_bound'").fetchone()[0])
    with kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'worker_bound', bound, run_id=context['run_id'])
        kb._append_event(conn, context['task_id'], 'worker_result',
                         {'pid': bound['pid'], 'claim_lock': context['claim_lock'], 'exit_code': 1,
                          'death_proof_version': 1, **result_identity_fields(conn, context)}, run_id=context['run_id'])
    assert proof.failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock'])
