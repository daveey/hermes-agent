"""A gateway drain suppresses new claims, and diagnostic logs belong to exactly one run."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (tmp_path / 'config.yaml').write_text(_managed_yaml())
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'profile_exists', lambda name: True)
    return tmp_path


@pytest.mark.parametrize('state', [dict(gateway_state='draining'), dict(restart_requested=True)])
def test_drain_suppresses_claims_but_resumes_when_running(home, monkeypatch, state):
    from gateway import status
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda record: True)
    path = home / 'gateway_state.json'
    path.write_text(json.dumps(state))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='waiting for owner', assignee='default')
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == 'ready' and task.current_run_id is None
        path.write_text(json.dumps(dict(gateway_state='running', restart_requested=False)))
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id))
        assert spawned == [tid]


def test_run_marker_excludes_old_output_even_without_session_or_trailer(home, monkeypatch):
    raw = ('Old force-push approval text\n[kanban-worker-exit] rc=0\n'
           'Old buffered reply after trailer\n[kanban-worker-start] run_id=42\n'
           'Kanban owner refused: Gateway draining: restart_wait\n[kanban-worker-exit] rc=69\n')
    monkeypatch.setattr(kb, 'read_worker_log', lambda *a, **k: raw)
    out = dispatch._worker_final_output('t_x', run_id=42)
    assert out == 'Kanban owner refused: Gateway draining: restart_wait'
    assert dispatch._worker_final_output('t_x', run_id=43) == ''


def test_dispatcher_marks_run_before_any_client_output(home):
    task = SimpleNamespace(id='t_x', current_run_id=42)
    with dispatch._open_worker_log(task, None) as log:
        log.write(b'current failure\n')
    text = kb.read_worker_log('t_x')
    assert text is not None
    assert text.index('[kanban-worker-start] run_id=42') < text.index('current failure')


def test_long_current_reply_retains_diagnostic_tail(home):
    with dispatch._open_worker_log(SimpleNamespace(id='t_x', current_run_id=42), None) as log:
        log.write(('x' * 8000 + '\nCURRENT REASON\n[kanban-worker-exit] rc=1\n').encode())
    assert dispatch._worker_final_output('t_x', run_id=42).endswith('CURRENT REASON')
    assert dispatch._worker_log_exit_code('t_x', run_id=42) == 1


def test_log_from_before_the_marker_deploy_still_yields_its_exit_code(home):
    # A worker spawned by the pre-marker dispatcher: no marker anywhere, so fall back to the trailer cut.
    from hermes_cli.kanban_db import worker_log_path
    path = worker_log_path('t_old', board=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('earlier run\n[kanban-worker-exit] rc=1\nSession: s\nrefused\n[kanban-worker-exit] rc=69\n')
    assert dispatch._worker_log_exit_code('t_old', run_id=7) == 69
    assert dispatch._worker_final_output('t_old', run_id=7).endswith('refused')


def test_silent_new_run_does_not_inherit_old_interruption_exit(home, monkeypatch):
    monkeypatch.setattr(dispatch, '_classify_worker_exit', lambda pid: ('unknown', None))
    with dispatch._open_worker_log(SimpleNamespace(id='t_x', current_run_id=41), None) as log:
        log.write(b'[kanban-worker-exit] rc=69\n')
    with dispatch._open_worker_log(SimpleNamespace(id='t_x', current_run_id=42), None):
        pass
    assert dispatch._worker_log_exit_code('t_x', run_id=42) is None
    dead = dispatch._classify_dead_worker(99999999, 'owner', task_id='t_x', run_id=42)
    assert dead.event_kind == 'crashed'


@pytest.mark.parametrize('reply', ['CURRENT FAILURE', ''])
def test_crash_notice_never_inherits_previous_run(home, monkeypatch, reply):
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(dispatch, '_classify_worker_exit', lambda pid: ('nonzero_exit', 1))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='current output only', assignee='default')
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert task is not None
        log_path = kb.worker_log_path(tid)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('OLD FORCE PUSH APPROVAL\n[kanban-worker-exit] rc=0\nOLD BUFFERED OUTPUT\n')
        with dispatch._open_worker_log(task, None) as log:
            log.write(reply.encode())
        dispatch._set_worker_pid(conn, tid, 99999999)
        assert tid in dispatch.detect_crashed_workers(conn)
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='crashed'", (tid,)).fetchone()
        payload = json.loads(event[0])
        assert payload.get('worker_output', '') == reply
        run = conn.execute('SELECT error FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
        assert 'OLD' not in run[0]


def test_dead_drain_snapshot_does_not_stop_new_work(home, monkeypatch):
    from gateway import status
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda record: False)
    (home / 'gateway_state.json').write_text(json.dumps({'gateway_state': 'draining'}))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='dead owner', assignee='default')
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id))
        assert spawned == [tid]


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_assignee_owner_drain_blocks_claim_before_spawn(home, monkeypatch, lane):
    from gateway import status
    from hermes_cli import profiles
    owner = home / 'profiles' / 'other'
    owner.mkdir(parents=True)
    (owner / 'gateway_state.json').write_text(json.dumps({'restart_requested': True}))
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda record: True)
    monkeypatch.setattr(profiles, 'resolve_profile_env', lambda name: str(owner))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='profile drain', assignee='other')
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (lane, tid))
        conn.commit()
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == lane and task.current_run_id is None
