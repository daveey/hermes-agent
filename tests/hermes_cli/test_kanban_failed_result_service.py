"""Real manager custody proofs; no production units or boards are touched."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_failed_result_observer import arm
from tools.environments.local import build_subprocess_env
from tools.process_registry import systemd_user_bus_env
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


def _systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True,
                          text=True, encoding='utf-8', timeout=10, check=False,
                          env=build_subprocess_env(extra=systemd_user_bus_env()))


def _property(unit, name):
    result = _systemctl('show', unit, f'--property={name}', '--value')
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('condition did not become true within deadline')


@pytest.mark.parametrize('missing', ['board', 'run'])
def test_observer_does_not_recreate_removed_target(board, missing, tmp_path):
    from hermes_cli.kanban_failed_result_observer import observe
    _conn, context = board
    context = dict(context)
    if missing == 'board':
        context['db'] = str(tmp_path / 'removed' / 'board.db')
    else:
        context['run_id'] = -1
    context['ready_path'] = str(tmp_path / 'absent-ready-dir' / 'ready')
    context['observer_unit'] = 'fixture.service'
    observe(context)
    assert not Path(context['ready_path']).exists()
    if missing == 'board':
        assert not Path(context['db']).exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='real Linux systemd custody')
def test_observer_survives_worker_cgroup_teardown(board):
    if _systemctl('show-environment').returncode:
        pytest.skip('no reachable user manager')
    conn, context = board
    # A disposable finite service is a real owner cgroup with teardown semantics.
    # It expires naturally; this fixture never stops/signals any systemd unit.
    outer_unit = f'hermes-fixture-failed-result-{time.monotonic_ns()}.service'
    env = build_subprocess_env(extra=systemd_user_bus_env())
    launch = subprocess.run(['systemd-run', '--user', '--quiet', '--collect',
                             f'--unit={outer_unit}', '--', '/bin/sleep', '10'],
                            capture_output=True, text=True, encoding='utf-8', timeout=10,
                            env=env, check=False)
    assert launch.returncode == 0, launch.stderr
    outer_pid = int(_wait_for(lambda: (pid := _property(outer_unit, 'MainPID')) not in ('', '0') and pid))
    kbd._set_worker_pid(conn, context['task_id'], outer_pid)
    unit = arm(context)
    assert isinstance(unit, str) and unit.endswith('.service')
    observer_pid = int(_property(unit, 'MainPID'))
    observer_group = _property(unit, 'ControlGroup')
    outer_group = _property(outer_unit, 'ControlGroup')
    assert observer_group != outer_group
    assert '/app.slice/' in observer_group
    assert 'hermes-kanban.slice' not in observer_group
    assert _property(unit, 'Restart') == 'on-failure'
    assert Path(f'/proc/{observer_pid}/cgroup').read_text(encoding='utf-8').strip().endswith(observer_group)
    fixtures._failed_child(conn, context, outer_pid=outer_pid)
    assert conn.execute('SELECT status FROM task_runs WHERE id=?',
                        (context['run_id'],)).fetchone()[0] == 'running'
    _wait_for(lambda: not Path(f'/proc/{outer_pid}').exists(), seconds=15)
    _wait_for(lambda: conn.execute('SELECT status FROM task_runs WHERE id=?',
                                  (context['run_id'],)).fetchone()[0] == 'crashed', seconds=5)
    assert len(conn.execute("SELECT 1 FROM task_events WHERE run_id=? AND kind='crashed'",
                            (context['run_id'],)).fetchall()) == 1
    kbd.detect_crashed_workers(conn)
    assert len(conn.execute("SELECT 1 FROM task_events WHERE run_id=? AND kind='crashed'",
                            (context['run_id'],)).fetchall()) == 1
