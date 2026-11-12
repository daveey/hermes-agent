"""Worker pressure applies even when hub CPU admission is disabled."""
import json

import pytest

from tests.hermes_cli.test_kanban_hub_admission_grant_echo import Rig, claim
from hermes_cli import kanban_admission as admission
from hermes_cli import worker_compute_capacity as capacity


@pytest.fixture
def worker_rig(monkeypatch, tmp_path):
    rig = Rig(tmp_path, monkeypatch, cap=2)
    monkeypatch.setattr(capacity, 'policy_root', lambda: rig.home)
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
    monkeypatch.setattr('os.getloadavg', lambda: (1, 1, 1))
    pool = tmp_path / 'pool'
    pool.mkdir()
    monkeypatch.setattr(capacity, 'pool_path', lambda: pool)
    try:
        yield rig, pool
    finally:
        rig.close()


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_final_gate_counts_new_detached_jobs_and_releases_grant(worker_rig, lane):
    rig, pool = worker_rig
    rig.fill(1)
    t = rig.card(lane)
    def populated_after_lease(conn, task, gate):
        group = pool / 'unknown.scope'
        group.mkdir()
        (group / 'cgroup.events').write_text('populated 1\n')
    seen = rig.echo_between_gate_and_txn(populated_after_lease)
    assert claim(rig.conn, t, lane) is None
    assert seen['gate'].consumed is False
    from hermes_cli import kanban_db_sync as sync
    assert sync.pop_claim_deferral(t) == 'worker_capacity_full'


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_worker_own_grant_echo_does_not_take_extra_slot(worker_rig, lane):
    rig, pool = worker_rig
    started = rig.fill(1)
    name = capacity.owner_slice('default', started[0])
    (pool / name).mkdir()
    (pool / name / 'cgroup.events').write_text('populated 1\n')
    directory = rig.home / 'worker-resources' / 'owners'
    directory.mkdir()
    (directory / f'{name}.json').write_text(json.dumps(['default', started[0]]))
    t = rig.card(lane)
    rig.echo_between_gate_and_txn()
    assert claim(rig.conn, t, lane) is not None


def test_reporter_counts_detached_jobs_and_unknowns(worker_rig):
    from hermes_cli import machine_metrics
    rig, pool = worker_rig
    rig.fill(1)
    group = pool / 'detached.scope'
    group.mkdir()
    events = group / 'cgroup.events'
    events.write_text('populated 1\n')
    assert machine_metrics.collect(rig.home)['active_card_runs'] == 2
    events.write_text('populated 0\n')
    assert machine_metrics.collect(rig.home)['active_card_runs'] == 1
    events.unlink()
    assert machine_metrics.collect(rig.home)['active_card_runs'] is None


def test_persistent_hysteresis_and_bad_state_refuse(worker_rig, monkeypatch):
    from hermes_cli.worker_resource_admission import refusal
    rig, _ = worker_rig
    for load, expected in [(24, 'worker_load_pressure'), (20, 'worker_load_pressure'), (16, None)]:
        monkeypatch.setattr('os.getloadavg', lambda: (load, 0, 0))
        assert refusal() == expected
    (rig.home / 'worker-resources' / 'pressure.json').write_text('{broken')
    assert refusal() == 'worker_resources_unknown'


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_parked_job_blocks_before_lease_then_releases(monkeypatch, tmp_path, lane):
    rig = Rig(tmp_path, monkeypatch, cap=1)
    monkeypatch.setattr(capacity, 'policy_root', lambda: rig.home)
    try:
        monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
        monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
        monkeypatch.setattr('os.getloadavg', lambda: (1, 1, 1))
        pool = tmp_path / 'cgroups'
        pool.mkdir()
        monkeypatch.setattr(capacity, 'pool_path', lambda: pool, raising=False)
        owner = ('default', 't_parked')
        unit = capacity.owner_slice(*owner)
        (pool / unit).mkdir()
        events = pool / unit / 'cgroup.events'
        events.write_text('populated 1\n')
        directory = rig.home / 'worker-resources' / 'owners'
        directory.mkdir(parents=True)
        (directory / f'{unit}.json').write_text(json.dumps(owner))
        t = rig.card(lane)
        assert claim(rig.conn, t, lane) is None
        from hermes_cli import kanban_db_sync as sync
        assert sync.pop_claim_deferral(t) == 'worker_capacity_full'
        assert rig.row(t)['status'] == lane
        events.write_text('populated 0\n')
        assert claim(rig.conn, t, lane) is not None
    finally:
        rig.close()


def test_absent_pool_is_empty_only_with_readable_manager(tmp_path):
    from hermes_cli.worker_resource_admission import pool_occupancy
    manager = tmp_path / 'manager'
    manager.mkdir()
    pool = manager / 'hermes.slice' / 'hermes-kanban.slice'
    with pytest.raises(FileNotFoundError):
        pool_occupancy(pool, {}, set())
    (manager / 'cgroup.events').write_text('populated 1\n')
    assert pool_occupancy(pool, {}, {('default', 't_live')}) == {('default', 't_live')}
    pool.mkdir(parents=True)
    (pool / 'broken.scope').mkdir()
    with pytest.raises(FileNotFoundError):
        pool_occupancy(pool, {}, set())


def test_malformed_root_config_refuses_without_raising(monkeypatch, tmp_path):
    from hermes_cli.worker_resource_admission import refusal
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    for text in ['kanban: [broken', '- not-a-mapping', 'kanban: 7']:
        (tmp_path / 'config.yaml').write_text(text)
        assert refusal() == 'worker_resources_unknown'


def test_worker_brake_is_not_bypassed_by_disabled_hub(monkeypatch, tmp_path):
    from hermes_cli import machine_cpu_metrics as cpu
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    monkeypatch.setattr(cpu, 'root_limit', lambda: (cpu.OFF, None))
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
    monkeypatch.setattr('os.getloadavg', lambda: (24, 20, 18))
    assert admission.hub_cpu_advisory('t_pressure') == 'worker_load_pressure'
    # Final boundary independently refuses, with hub admission still OFF.
    assert admission._final_hub_gate(None)[0] == 'worker_load_pressure'
    state = tmp_path / 'worker-resources' / 'pressure.json'
    assert json.loads(state.read_text())['held'] is True


@pytest.mark.parametrize('expired,slow', [(True, False), (False, True), (False, False)])
def test_worker_only_final_deadline_and_budget(worker_rig, monkeypatch, expired, slow):
    import time
    from types import SimpleNamespace
    from hermes_cli import machine_cpu_metrics as cpu
    monkeypatch.setattr(cpu, 'root_limit', lambda: (cpu.OFF, None))
    gate = SimpleNamespace(expires=time.time() + (-10 if expired else 60))
    started = time.monotonic() - (10 if slow else 0)
    reason, _ = admission._final_hub_gate(gate, started)
    assert reason == ('worker_resources_unknown' if expired or slow else None)


def test_policy_enabled_after_lock_selection_refuses(worker_rig, monkeypatch):
    from hermes_cli import machine_cpu_metrics as cpu
    rig, _ = worker_rig
    monkeypatch.setattr(cpu, 'root_limit', lambda: (cpu.OFF, None))
    observations = iter([False, True])
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: next(observations))
    with admission.claim_transaction(rig.conn) as accepted:
        assert not accepted

