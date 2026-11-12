"""CPU containment is verified before admitting a managed interpreter."""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import worker_compute_capacity as capacity


def test_effective_cores_uses_host_cpuset_not_per_card_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity, 'CGROUP_ROOT', tmp_path)
    monkeypatch.setattr(os, 'sched_getaffinity', lambda _: set(range(16)))
    (tmp_path / 'cpuset.cpus.effective').write_text('0-7,10,12-13\n')
    (tmp_path / 'cpu.max').write_text('600000 100000\n')
    assert capacity.effective_cores() == 6
    (tmp_path / 'cpu.max').write_text('max 100000\n')
    assert capacity.effective_cores() == 11


@pytest.mark.parametrize('raw', ['', '2-1', 'x', '0-3,2'])
def test_invalid_cpuset_refuses(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(capacity, 'CGROUP_ROOT', tmp_path)
    (tmp_path / 'cpuset.cpus.effective').write_text(raw)
    (tmp_path / 'cpu.max').write_text('max 100000')
    with pytest.raises(ValueError):
        capacity.effective_cores()


@pytest.fixture
def configured(monkeypatch, tmp_path):
    import hermes_constants
    import subprocess
    from tools import process_registry
    root = tmp_path / 'cgroups'
    monkeypatch.setattr(capacity, 'CGROUP_ROOT', root)
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: tmp_path / 'home')
    monkeypatch.setattr(process_registry, 'systemd_user_bus_env', lambda: {'bus': 'fixture'})
    base = root / 'user.slice' / f'user-{os.getuid()}.slice'
    pool = base / f'user@{os.getuid()}.service/hermes.slice/hermes-kanban.slice'
    card = pool / capacity.owner_slice('board', 'task')
    card.mkdir(parents=True)
    for path, quota in [(base, 14), (pool, 12), (card, 2)]:
        (path / 'cpu.max').write_text(f'{quota * 100000} 100000')
        (path / 'cpu.weight').write_text('10')
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: calls.append((a, kw)))
    return SimpleNamespace(base=base, pool=pool, card=card, home=tmp_path / 'home', calls=calls)


def test_configure_verifies_and_publishes_owner(configured):
    name = capacity.configure_card('board', 'task')
    assert name == configured.card.name
    assert json.loads((configured.home / 'worker-resources/owners' / f'{name}.json').read_text()) == ['board', 'task']
    assert len(configured.calls) == 4
    assert all(call[1]['env'] == {'bus': 'fixture'} for call in configured.calls)
    assert 'CPUQuota=1200%' in configured.calls[0][0][0]
    assert 'CPUQuota=200%' in configured.calls[2][0][0]


@pytest.mark.parametrize('part', ['base', 'pool', 'card'])
def test_unbounded_quota_never_publishes_owner(configured, part):
    (getattr(configured, part) / 'cpu.max').write_text('max 100000')
    with pytest.raises(ValueError):
        capacity.configure_card('board', 'task')
    assert not (configured.home / 'worker-resources/owners').exists()


def test_cpu_weight_readback_required(configured):
    (configured.card / 'cpu.weight').write_text('100')
    with pytest.raises(ValueError, match='weight'):
        capacity.configure_card('board', 'task')
    assert not (configured.home / 'worker-resources/owners').exists()


def test_managed_worker_requires_card_slice_and_sets_priority(monkeypatch, configured):
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    scope = configured.card / 'hermes-worker-kanban-task-run-1.scope'
    scope.mkdir()
    policy = SimpleNamespace(kanban_json=json.dumps({'task_id': 'task', 'board': 'board'}))
    assert capacity.managed_target(policy, scope) is True
    with pytest.raises(ValueError):
        capacity.managed_target(policy, configured.pool / scope.name)
    with pytest.raises(ValueError):
        capacity.managed_target(policy, None)
    observed = []
    monkeypatch.setattr(os, 'getpriority', lambda *a: 19)
    monkeypatch.setattr(os, 'setpriority', lambda *a: observed.append(a))
    capacity.lower_priority(123)
    assert observed == [(os.PRIO_PROCESS, 123, 19)]


def test_missing_bus_refuses_before_configuration(configured, monkeypatch):
    from tools import process_registry
    monkeypatch.setattr(process_registry, 'systemd_user_bus_env', lambda: None)
    with pytest.raises(process_registry.RestartSafeScopeUnavailable):
        capacity.configure_card('board', 'task')
    assert configured.calls == []
    assert not (configured.home / 'worker-resources/owners').exists()


def test_failed_systemctl_never_publishes_owner(configured, monkeypatch):
    import subprocess

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'systemctl')

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        capacity.configure_card('board', 'task')
    assert not (configured.home / 'worker-resources/owners').exists()


def test_changed_core_budget_revalidates_backstop(configured, monkeypatch):
    capacity.configure_card('board', 'task')
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 8)
    configured.calls.clear()
    with pytest.raises(ValueError, match='quota'):
        capacity.configure_card('board', 'task')
    assert configured.calls == []


def test_nonworker_launch_unchanged(monkeypatch):
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    assert capacity.managed_target(None, None) is False
