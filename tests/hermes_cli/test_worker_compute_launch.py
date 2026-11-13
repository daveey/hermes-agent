"""The whole worker scope must share its durable per-card CPU slice."""
from unittest.mock import patch
from types import SimpleNamespace
import pytest


def test_persisted_worker_job_refuses_unavailable_bus(monkeypatch):
    from tools import process_registry as pr
    from tools import process_registry_persist_scope as persist
    from hermes_cli import worker_compute_capacity as capacity

    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(pr, '_IS_LINUX', True)
    monkeypatch.setattr(persist, 'kanban_run_scope_cgroup', lambda: None)
    monkeypatch.setattr(persist, 'user_unit_cgroup', lambda: None)
    with pytest.raises(pr.RestartSafeScopeUnavailable):
        persist.persisted_job_scope_argv(SimpleNamespace(persist_on_release=True), ['sleep', '1'], 'job')


def test_persisted_job_retains_owning_card_slice(monkeypatch, tmp_path):
    from tools import process_registry as pr
    from tools import process_registry_persist_scope as persist
    from hermes_cli.worker_compute_capacity import owner_slice

    card_slice = owner_slice('board', 'task')
    unit = tmp_path / card_slice / 'hermes-worker-kanban-task-run-1.scope'
    unit.mkdir(parents=True)
    (unit / 'memory.max').write_text(str(4 * 1024 ** 3))
    monkeypatch.setattr(persist, 'kanban_run_scope_cgroup', lambda: unit)
    monkeypatch.setattr(persist.shutil, 'which', lambda _: '/usr/bin/systemd-run')
    monkeypatch.setattr(pr, '_systemd_run_user_scope_available', lambda: True)
    monkeypatch.setattr(pr, '_IS_LINUX', True)
    session = SimpleNamespace(persist_on_release=True, systemd_unit=None)
    got = persist.persisted_job_scope_argv(session, ['sleep', '1'], 'job')
    assert f'--slice={card_slice}' in got


def test_worker_launch_retains_memory_pool_and_uses_card_cpu_slice():
    from hermes_cli import kanban_aggregate_scope as scope
    from hermes_cli.worker_compute_capacity import owner_slice

    argv = ['systemd-run', '--user', '--scope', '--', 'python', '-m', 'agent.managed_worker']
    scope.configure_pool.cache_clear()
    with patch('hermes_cli.worker_compute_capacity.worker_enabled', return_value=True), \
         patch('hermes_cli.worker_compute_capacity.configure_card', return_value=owner_slice('board', 'task')) as configure, \
         patch('subprocess.run'):
        got = scope.bounded_scope(argv, task_id='task', board='board')
    configure.assert_called_once_with('board', 'task')
    assert f'--slice={owner_slice("board", "task")}' in got[:got.index('--')]
    assert got[got.index('--') + 1:] == ['nice', '-n', '15', 'python', '-m', 'agent.managed_worker']
    scope.configure_pool.cache_clear()
