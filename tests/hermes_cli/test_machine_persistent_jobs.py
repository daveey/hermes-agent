"""Persistent-work safety observations must fail closed across Board downgrades."""
import pytest

from hermes_cli import machine_drain_metrics as drain
from hermes_cli.kanban_sync_client import HubError


@pytest.mark.platforms('linux')
def test_policy_off_still_observes_populated_detached_scope(tmp_path, monkeypatch):
    from hermes_cli import machine_metrics as metrics, worker_compute_capacity as capacity
    pool = tmp_path / 'pool'
    pool.mkdir()
    scope = pool / 'parked.scope'
    scope.mkdir()
    (scope / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    monkeypatch.setattr(capacity, 'pool_path', lambda: pool)
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: False)
    monkeypatch.setattr(metrics, 'active_card_runs', lambda **kw: 0)
    result = metrics.collect(tmp_path)
    assert result['active_card_runs'] == 0
    assert result['persistent_jobs'] == 1
    assert result['persistent_jobs_at_ms'] > 0


@pytest.mark.parametrize('events', ['populated 0\n', 'populated bad\n', 'frozen 0\n', None])
def test_empty_or_unreadable_pool_never_proves_unscoped_work_absent(tmp_path, monkeypatch, events):
    from hermes_cli import machine_persistent_jobs as jobs, worker_compute_capacity as capacity
    pool = tmp_path / 'pool'
    pool.mkdir()
    scope = pool / 'parked.scope'
    scope.mkdir()
    if events is not None:
        (scope / 'cgroup.events').write_text(events)
    monkeypatch.setattr(capacity, 'pool_path', lambda: pool)
    assert jobs.collect() == {}


@pytest.mark.parametrize('stamp', [None, True, 0, -1, 10001, 7999])
def test_sender_drops_invalid_or_old_observation_without_restamping(monkeypatch, stamp):
    from hermes_cli import machine_persistent_jobs as jobs
    monkeypatch.setattr(jobs.time, 'time', lambda: 10)
    sample = {'active_card_runs': 0, 'persistent_jobs': 0, 'persistent_jobs_at_ms': stamp}
    seen = []
    drain.send_compatible(seen.append, sample)
    assert seen == [{'active_card_runs': 0}]


@pytest.mark.parametrize('count,stamp', [(0, 9000), (100000, 8000)])
def test_sender_keeps_fresh_observation_stamp(monkeypatch, count, stamp):
    from hermes_cli import machine_persistent_jobs as jobs
    monkeypatch.setattr(jobs.time, 'time', lambda: 10)
    sample = {'persistent_jobs': count, 'persistent_jobs_at_ms': stamp}
    seen = []
    drain.send_compatible(seen.append, sample)
    assert seen == [sample]


def test_persistent_pair_rejection_preserves_counters_without_replaying_safety(monkeypatch):
    from hermes_cli import machine_persistent_jobs as jobs
    monkeypatch.setattr(jobs.time, 'time', lambda: 1)
    calls, rejected = [], []
    sample = {"active_card_runs": 0, "persistent_jobs": 0, "persistent_jobs_at_ms": 1000,
              "tokens_io_total": 42, "tokens_by_model": {"model": [42, 0]},
              "cpu_busy_pct": 1, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1000}

    def send(payload):
        calls.append(dict(payload))
        if "persistent_jobs" in payload:
            raise HubError(400, "unknown field(s): persistent_jobs, persistent_jobs_at_ms")
        return "ok"

    assert drain.send_compatible(send, sample, lambda: rejected.append(True)) == "ok"
    assert len(calls) == 2
    assert calls[-1] == {"active_card_runs": 0, "tokens_io_total": 42,
                         "tokens_by_model": {"model": [42, 0]}}
    assert rejected == []
    assert sample["persistent_jobs"] == 0


@pytest.mark.parametrize('count', [None, True, -1, 100001, 0.0, '0'])
def test_bad_count_drops_whole_pair(monkeypatch, count):
    from hermes_cli import machine_persistent_jobs as jobs
    monkeypatch.setattr(jobs.time, 'time', lambda: 1)
    assert jobs.fresh({'persistent_jobs': count, 'persistent_jobs_at_ms': 1000}) == {}


@pytest.mark.parametrize('stamp', [True, 0, -1, 1.0])
def test_stamp_type_and_positive_bound_at_early_clock(monkeypatch, stamp):
    from hermes_cli import machine_persistent_jobs as jobs
    monkeypatch.setattr(jobs.time, 'time', lambda: 1)
    assert jobs.fresh({'persistent_jobs': 1, 'persistent_jobs_at_ms': stamp}) == {}


@pytest.mark.platforms('linux')
def test_slow_census_drops_observation(tmp_path, monkeypatch):
    from hermes_cli import machine_persistent_jobs as jobs, worker_compute_capacity as capacity
    monkeypatch.setattr(capacity, 'pool_path', lambda: tmp_path)
    clock = [1.0]

    def slow_census(*args):
        clock[0] += 2.001
        return {('board', 'job')}

    monkeypatch.setattr(capacity, 'occupied_owners', slow_census)
    monkeypatch.setattr(jobs.time, 'time', lambda: clock[0])
    assert jobs.collect() == {}


@pytest.mark.parametrize('generation', [0, 1, 2, 3])
def test_all_old_board_generations_keep_safety_unknown(monkeypatch, generation):
    from hermes_cli import machine_persistent_jobs as jobs
    monkeypatch.setattr(jobs.time, 'time', lambda: 1)
    calls, rejected = [], []
    fields = ['drain_active', 'tokens_io_total', 'tokens_by_model', 'persistent_jobs']
    sample = {'active_card_runs': 0, 'drain_active': 0, 'tokens_io_total': 42,
              'tokens_by_model': {}, 'persistent_jobs': 0, 'persistent_jobs_at_ms': 1000}

    def send(payload):
        calls.append(dict(payload))
        if any(key in payload for key in fields[generation:]):
            raise HubError(400, 'unknown field')
        return 'ok'

    assert drain.send_compatible(send, sample, lambda: rejected.append(True)) == 'ok'
    assert 'persistent_jobs' not in calls[-1]
    assert 'persistent_jobs_at_ms' not in calls[-1]
    assert rejected == ([True] if generation < 2 else [])
