"""Observer protocol boundaries on disposable boards; no live service mutation."""
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failed_result_observer as observer
from hermes_cli import kanban_failed_result_service as service
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


class EndPoll(Exception):
    pass


def _ready_context(context, tmp_path):
    return {**context, 'ready_path': str(tmp_path / 'ready'), 'observer_unit': 'fixture.service',
            'writer_pid': os.getpid(), 'writer_birth': dispatch._process_fingerprint(os.getpid())}


def test_idle_observer_acknowledges_without_board_writes(board, tmp_path, monkeypatch):
    _, context = board
    context = _ready_context(context, tmp_path)
    statements = []
    original = observer.connect_existing
    @contextlib.contextmanager
    def traced(path):
        with original(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(observer, 'connect_existing', traced)
    def end_poll(seconds):
        assert seconds == observer.POLL_SECONDS
        raise EndPoll
    monkeypatch.setattr(observer.time, 'sleep', end_poll)
    with pytest.raises(EndPoll):
        observer.observe(context)
    assert Path(context['ready_path']).read_text() == context['observer_unit']
    assert not any(sql.upper().startswith(('BEGIN', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for sql in statements)


def test_observer_carries_explicit_nondefault_board(board, tmp_path, monkeypatch):
    conn, context = board
    fixtures._failed_child(conn, context)
    context = {**_ready_context(context, tmp_path), 'board': 'alternate'}
    original = dispatch.detect_crashed_workers
    calls = []
    def sweep(connection, **kwargs):
        calls.append(kwargs)
        return original(connection, **kwargs)
    monkeypatch.setattr(dispatch, 'detect_crashed_workers', sweep)
    observer.observe(context)
    assert calls == [{'board': 'alternate', 'failed_results_only': True}]
    assert conn.execute('SELECT status FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()[0] == 'crashed'


def test_failed_arm_keeps_result_and_records_diagnostic(board, monkeypatch):
    conn, context = board
    dispatch._set_worker_pid(conn, context['task_id'], os.getpid())
    with kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'worker_bound',
                         {'pid': os.getpid(), 'started_at': dispatch._process_fingerprint(os.getpid()),
                          'claim_lock': context['claim_lock']}, run_id=context['run_id'])
    def refuse(ctx):
        assert ctx == context
        assert not conn.in_transaction
        assert conn.execute("SELECT 1 FROM task_events WHERE kind='worker_result'").fetchone() is None
        raise RuntimeError('fixture manager unavailable')
    monkeypatch.setattr(observer, 'arm', refuse)
    session_kanban._record_worker_result(context, 1, 'fixture failure')
    rows = conn.execute("SELECT kind,payload FROM task_events WHERE kind LIKE 'worker_result%' ORDER BY id").fetchall()
    assert [row['kind'] for row in rows] == ['worker_result_observer_failed', 'worker_result']
    assert json.loads(rows[0]['payload'])['error'] == 'fixture manager unavailable'
    assert json.loads(rows[1]['payload'])['exit_code'] == 1
    assert json.loads(rows[1]['payload'])['death_proof_version'] == 1
    assert conn.execute('SELECT status FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()[0] == 'running'


@pytest.mark.parametrize('ack', ['correct', 'wrong', 'none', 'refused'])
def test_service_custody_requires_exact_readiness_ack(board, monkeypatch, ack):
    _, context = board
    monkeypatch.setattr(service, 'locate_command', lambda name: SimpleNamespace(command=('/usr/bin/systemd-run',)))
    times = iter([0, 0, 2])
    monkeypatch.setattr(service.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)
    calls = []
    def submit(command, **kwargs):
        from types import SimpleNamespace
        calls.append(command)
        payload = json.loads(command[-1])
        if ack in ('correct', 'wrong'):
            Path(payload['ready_path']).write_text(payload['observer_unit'] if ack == 'correct' else 'other.service')
        return SimpleNamespace(returncode=1 if ack == 'refused' else 0, stderr='fixture refusal')
    monkeypatch.setattr(service.subprocess, 'run', submit)
    if ack == 'correct':
        assert service.arm_service(context, startup_seconds=1).endswith('.service')
    else:
        with pytest.raises(RuntimeError, match='refused|acknowledge'):
            service.arm_service(context, startup_seconds=1)
    assert '--property=Slice=app.slice' in calls[0]
    assert '--property=Restart=on-failure' in calls[0]
    assert not any(arg.startswith('--setenv=HERMES_KANBAN_TASK=') for arg in calls[0])


def test_missing_manager_is_not_reported_as_custody(board, monkeypatch):
    _, context = board
    monkeypatch.setattr(service, 'locate_command', lambda name: SimpleNamespace(command=()))
    with pytest.raises(RuntimeError, match='needs systemd-run'):
        service.arm_service(context)


def _armed_properties(context, monkeypatch):
    monkeypatch.setattr(service, 'locate_command', lambda name: SimpleNamespace(command=('/usr/bin/systemd-run',)))
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)
    calls = []
    def submit(command, **kwargs):
        calls.append(command)
        payload = json.loads(command[-1])
        Path(payload['ready_path']).write_text(payload['observer_unit'])
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(service.subprocess, 'run', submit)
    service.arm_service(context, startup_seconds=1)
    props = {}
    for arg in calls[0][:calls[0].index('--')]:
        if arg.startswith('--property='):
            key, _, value = arg.removeprefix('--property=').partition('=')
            props[key] = value
    return props


def test_armed_unit_restarts_are_bounded(board, monkeypatch):
    """[daveey] A deterministic startup failure (``No module named 'ruamel'``) looped
    forever under ``StartLimitIntervalSec=0``. The unit must retire itself."""
    _, context = board
    props = _armed_properties(context, monkeypatch)
    assert props['Restart'] == 'on-failure'
    interval = props['StartLimitIntervalSec']
    assert interval.endswith('s') and int(interval[:-1]) > 0
    assert 0 < int(props['StartLimitBurst']) <= 20
    assert str(observer.STARTUP_FAILURE_EXIT) in props['RestartPreventExitStatus'].split()


@pytest.mark.parametrize('argv', [['observer'], ['observer', 'not json'], ['observer', '[]'],
                                  ['observer', '{"db": "/x"}']])
def test_bad_context_retires_with_prevent_exit(argv):
    assert observer.main(argv) == observer.STARTUP_FAILURE_EXIT


def test_import_failure_retires_with_prevent_exit(board, tmp_path, monkeypatch):
    _, context = board
    context = _ready_context(context, tmp_path)
    def broken(_):
        raise ModuleNotFoundError("No module named 'ruamel'")
    monkeypatch.setattr(observer, 'observe', broken)
    assert observer.main(['observer', json.dumps(context)]) == observer.STARTUP_FAILURE_EXIT


def test_other_observer_errors_still_restart(board, tmp_path, monkeypatch):
    _, context = board
    def broken(_):
        raise RuntimeError('transient')
    monkeypatch.setattr(observer, 'observe', broken)
    with pytest.raises(RuntimeError):
        observer.main(['observer', json.dumps(_ready_context(context, tmp_path))])


def test_subprocess_import_failure_exits_prevent_status(tmp_path):
    """The real ``-m`` entrypoint maps a lazy import failure to the prevent code."""
    import subprocess
    import sys
    db = tmp_path / 'kanban.db'
    db.write_bytes(b'')
    context = {'db': str(db), 'task_id': 't', 'run_id': 1, 'claim_lock': 'x'}
    root = Path(__file__).resolve().parents[2]
    # Block a lazily imported observer dependency: importing it raises ImportError.
    driver = ("import runpy, sys; sys.modules['hermes_cli.kanban_dispatch_owner'] = None; "
              f"sys.argv = ['observer', sys.argv[1]]; runpy.run_module({observer.__name__!r}, run_name='__main__')")
    proc = subprocess.run([sys.executable, '-c', driver, json.dumps(context)],
                          cwd=root, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == observer.STARTUP_FAILURE_EXIT, proc.stderr[-500:]
    assert 'retiring' in proc.stderr
