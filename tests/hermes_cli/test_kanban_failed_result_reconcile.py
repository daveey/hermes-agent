"""Failed managed results must settle even with no owner dispatcher ticks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_connect import connect_closing
from tools.environments.local import build_subprocess_env


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'kanban'))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    path = kb.kanban_db_path(board='default')
    kb.init_db()
    with connect_closing(path) as conn:
        tid = kb.create_task(conn, title='failed result fixture', assignee='default')
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert task is not None
        yield conn, {'db': str(path), 'task_id': tid, 'run_id': task.current_run_id,
                     'claim_lock': task.claim_lock}


# stdin is a barrier: the parent records the real child's spawn identity first.
_CHILD = '''
import json, os, sys
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_connect import connect_closing
from gateway.session_kanban import _record_worker_result
context = json.loads(sys.argv[1])
if sys.argv[2] != 'observe':
    import hermes_cli.kanban_failed_result_observer as observer
    observer.arm = lambda context: None
sys.stdin.read(1)
with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
    kb._append_event(conn, context['task_id'], 'worker_bound',
        {'pid': os.getpid(), 'started_at': kbd._process_fingerprint(os.getpid()),
         'claim_lock': context['claim_lock']}, run_id=context['run_id'])
_record_worker_result(context, 1, 'fixture worker_receipt_timeout')
raise SystemExit(1)
'''


def _failed_child(conn, context, *, observe=False, outer_pid=None):
    proc = subprocess.Popen(
        [sys.executable, '-c', _CHILD, json.dumps(context), 'observe' if observe else 'unit'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', env=build_subprocess_env(),
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    try:
        kbd._set_worker_pid(conn, context['task_id'], outer_pid or proc.pid)
        stdout, stderr = proc.communicate('x', timeout=30)
        assert proc.returncode == 1, (stdout, stderr)
        result = conn.execute(
            "SELECT id, created_at, payload FROM task_events WHERE run_id=? AND kind='worker_result'",
            (context['run_id'],)).fetchone()
        assert result is not None, (stdout, stderr)
        assert json.loads(result['payload'])['exit_code'] == 1
        return result
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize('entry', ['stalled_owner', 'dispatcher'])
def test_failed_child_reconciles_without_owner_progress(board, entry):
    conn, context = board
    result = _failed_child(conn, context, observe=entry == 'stalled_owner')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if entry == 'dispatcher':
            kbd.detect_crashed_workers(conn)
        run = conn.execute('SELECT status, ended_at FROM task_runs WHERE id=?',
                           (context['run_id'],)).fetchone()
        if run['status'] != 'running':
            break
        time.sleep(.05)
    assert run['status'] != 'running', dict(run)
    assert run['ended_at'] >= result['created_at']
    # A subsequent healthy tick cannot close/account the same failed run twice.
    kbd.detect_crashed_workers(conn)
    crashes = conn.execute("SELECT id, payload FROM task_events WHERE run_id=? AND kind='crashed'",
                           (context['run_id'],)).fetchall()
    assert len(crashes) == 1
    payload = json.loads(crashes[0]['payload'])
    assert payload['source_result_event_id'] == result['id']
    assert payload['result_recorded_at'] == result['created_at']
    assert payload['reconciled_at'] >= result['created_at']
    assert payload['reconciled_by'] == ('dispatcher' if entry == 'dispatcher' else 'observer')


def test_observer_dispatcher_race_closes_and_accounts_once(board):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    conn, context = board
    _failed_child(conn, context)
    barrier = Barrier(2)
    def sweep(observer):
        with connect_closing(Path(context['db'])) as connection:
            barrier.wait(timeout=10)
            return kbd.detect_crashed_workers(connection, failed_results_only=observer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(sweep, [False, True]))
    assert sum(len(result) for result in results) == 1
    assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?',
                        (context['task_id'],)).fetchone()[0] == 1
    assert len(conn.execute("SELECT 1 FROM task_events WHERE run_id=? AND kind='crashed'",
                            (context['run_id'],)).fetchall()) == 1


def test_board_only_proof_accepts_real_dead_child(board):
    from hermes_cli.kanban_failed_result import failed_result_proof
    conn, context = board
    result = _failed_child(conn, context)
    proof = failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock'])
    assert proof is not None
    assert proof['source_result_event_id'] == result['id']
    assert proof['result_recorded_at'] == result['created_at']


@pytest.mark.parametrize('state', ['live', 'recycled', 'unreadable', 'denied', 'gone'])
def test_process_proof_fails_closed(monkeypatch, state):
    from hermes_cli import kanban_failed_result as reconcile
    def probe(pid, signal):
        assert (pid, signal) == (123, 0)
        if state == 'gone':
            raise ProcessLookupError()
        if state == 'denied':
            raise PermissionError()
    monkeypatch.setattr(reconcile.os, 'kill', probe)
    monkeypatch.setattr(reconcile.Path, 'read_text', lambda *args, **kwargs: '123 (fixture) S')
    monkeypatch.setattr(kbd, '_process_fingerprint',
                        lambda pid: {'live': 'epoch|1', 'recycled': 'epoch|2'}.get(state))
    assert reconcile.process_gone(123, 'epoch|1') is (state == 'gone')


@pytest.mark.parametrize('birth', [None, '', 'UNVERIFIED', 12])
def test_missing_birth_never_proves_death(monkeypatch, birth):
    from hermes_cli import kanban_failed_result as reconcile
    def unexpected(*args):
        pytest.fail('invalid binding must not query OS')
    monkeypatch.setattr(reconcile.os, 'kill', unexpected)
    assert not reconcile.process_gone(123, birth)


@pytest.mark.parametrize('change', ['run', 'claim', 'outer_live', 'bound_live', 'success', 'wrong_pid'])
def test_proof_rejects_changed_identity(board, monkeypatch, change):
    from hermes_cli import kanban_failed_result as reconcile
    conn, context = board
    _failed_child(conn, context)
    if change in ('run', 'claim'):
        context = {**context, 'run_id': -1} if change == 'run' else {**context, 'claim_lock': 'foreign:1'}
    elif change in ('outer_live', 'bound_live'):
        calls = iter([False] if change == 'outer_live' else [True, False])
        monkeypatch.setattr(reconcile, 'process_gone', lambda *args: next(calls))
    else:
        row = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind='worker_result'",
                           (context['run_id'],)).fetchone()
        payload = json.loads(row['payload'])
        payload['exit_code' if change == 'success' else 'pid'] = 0
        with kb.write_txn(conn):
            kb._append_event(conn, context['task_id'], 'worker_result', payload, run_id=context['run_id'])
    assert reconcile.failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']) is None


def test_observer_waits_for_distinct_outer_process(board):
    conn, context = board
    outer = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, env=build_subprocess_env())
    try:
        _failed_child(conn, context, observe=True, outer_pid=outer.pid)
        time.sleep(1)  # health: allow HX013 -- prove the observer does not close during a live-outer window
        assert conn.execute('SELECT status FROM task_runs WHERE id=?',
                            (context['run_id'],)).fetchone()[0] == 'running'
        assert outer.stdin is not None
        outer.stdin.close()
        outer.wait(timeout=5)
        deadline = time.monotonic() + 5
        state = 'running'
        while time.monotonic() < deadline:
            state = conn.execute('SELECT status FROM task_runs WHERE id=?',
                                 (context['run_id'],)).fetchone()[0]
            if state != 'running':
                break
            time.sleep(.05)
        assert state == 'crashed'
    finally:
        if outer.poll() is None:
            outer.kill()
            outer.wait()


@pytest.mark.parametrize('entry', ['dispatcher', 'observer'])
def test_failed_receipt_waits_for_real_bound_process(board, entry):
    conn, context = board
    outer = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read(1)'],
                             stdin=subprocess.PIPE, env=build_subprocess_env())
    child = None
    try:
        kbd._set_worker_pid(conn, context['task_id'], outer.pid)
        assert outer.stdin is not None
        outer.stdin.close()
        outer.wait(timeout=5)
        script = _CHILD.replace('sys.stdin.read(1)', 'pass').replace(
            'raise SystemExit(1)', "print('RESULT_COMMITTED', flush=True)\nsys.stdin.read(1)\nraise SystemExit(1)")
        child = subprocess.Popen(
            [sys.executable, '-c', script, json.dumps(context),
             'observe' if entry == 'observer' else 'unit'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=build_subprocess_env(), text=True, encoding='utf-8',
            cwd=str(Path(__file__).resolve().parents[2]))
        assert child.stdout is not None
        import select
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, 'child did not commit its result'
        assert child.stdout.readline().strip() == 'RESULT_COMMITTED'
        hold_until = time.monotonic() + 1
        while time.monotonic() < hold_until:
            if entry == 'dispatcher':
                kbd.detect_crashed_workers(conn)
            assert child.poll() is None
            assert conn.execute('SELECT status FROM task_runs WHERE id=?',
                                (context['run_id'],)).fetchone()[0] == 'running'
            time.sleep(.05)  # health: allow HX013 -- bounded negative observation while bound worker stays live
        child.communicate('x', timeout=5)
        deadline = time.monotonic() + 5
        state = 'running'
        while time.monotonic() < deadline:
            if entry == 'dispatcher':
                kbd.detect_crashed_workers(conn)
            state = conn.execute('SELECT status FROM task_runs WHERE id=?',
                                 (context['run_id'],)).fetchone()[0]
            if state != 'running':
                break
            time.sleep(.05)
        assert state == 'crashed'
    finally:
        for proc in (child, outer):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()


@pytest.mark.parametrize('entry', ['dispatcher', 'observer'])
@pytest.mark.parametrize('veto', ['bound_live', 'outer_recycled', 'bound_recycled', 'unknown_birth'])
def test_failed_receipt_veto_cannot_fall_back_to_legacy_reclaim(board, monkeypatch, entry, veto):
    from hermes_cli import kanban_failed_result as reconcile
    from hermes_cli import kanban_owner_recovery as owner
    conn, context = board
    _failed_child(conn, context)
    calls = []
    def gone(pid, birth):
        calls.append((pid, birth))
        return len(calls) == 1 if veto.startswith('bound') else False
    monkeypatch.setattr(reconcile, 'process_gone', gone)
    # Model legacy liveness accepting dead/recycled outer identity. It must not
    # override the stronger two-process failed-result veto, or consult state.db.
    monkeypatch.setattr(kbd, '_worker_alive', lambda *args: False)
    monkeypatch.setattr(owner, 'owner_reclaim_blocked', lambda *a, **k: False)
    monkeypatch.setattr(owner, 'apply_attempt_note', lambda *a, **k: None)
    kbd.detect_crashed_workers(conn, failed_results_only=entry == 'observer')
    assert conn.execute('SELECT status FROM task_runs WHERE id=?',
                        (context['run_id'],)).fetchone()[0] == 'running'
    assert not conn.execute("SELECT 1 FROM task_events WHERE run_id=? AND kind='crashed'",
                            (context['run_id'],)).fetchone()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux zombie identity proof')
def test_real_zombie_is_dead_only_with_matching_birth():
    from hermes_cli.kanban_failed_result import process_gone
    proc = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read(1)'],
                            stdin=subprocess.PIPE, env=build_subprocess_env())
    try:
        birth = kbd._process_fingerprint(proc.pid)
        assert birth is not None
        assert not process_gone(proc.pid, birth)
        assert proc.stdin is not None
        proc.stdin.write(b'x')
        proc.stdin.flush()
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        assert not process_gone(proc.pid, 'foreign|1')
        assert process_gone(proc.pid, birth)
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=5)
