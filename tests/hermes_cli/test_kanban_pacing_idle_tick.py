"""Dispatcher maintenance observes pools without local admissions."""
import json
import time

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from tests.hermes_cli import test_kanban_stuck_clock_holds as helpers
from tests.hermes_cli import test_kanban_notice_pacing_admission as notices

home = helpers.home
hub = notices.hub


@pytest.fixture(autouse=True)
def pacing_cache(monkeypatch):
    monkeypatch.setattr(pd, '_CACHE', {})
    monkeypatch.setattr(pd, '_WARNED', {})


def test_zero_admission_ticks_refresh_all_pools(home, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(pd.time, 'time', lambda: clock[0])
    monkeypatch.setattr(pd, 'load_config', lp.PacingConfig)
    reads = []

    def read(provider, now):
        reads.append((provider, now))
        return [lp.Reading(now, 'account', lp.SEVEN_DAY, 50, now + 86400)]

    monkeypatch.setattr(pd, 'read_pool', read)
    with kbc.connect() as conn:
        for offset in (0, 60, 300, 600, 900, 1200, 1800):
            clock[0] = 1800000000 + offset
            result = dispatch.dispatch_once(conn, max_spawn=0)
            assert not result.spawned
            assert not result.scheduling_admitted
            assert pd.state_path().exists(), 'idle tick never sampled the pools'
            state = json.loads(pd.state_path().read_text())
            for provider in lp.PacingConfig().providers:
                assert 0 <= clock[0] - state[provider]['read_at'] < 900
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert len(reads) == 6 * len(lp.PacingConfig().providers)


@pytest.mark.parametrize('disabled,dry_run', [(True, False), (False, True)])
def test_idle_refresh_respects_disable_and_dry_run(home, monkeypatch, disabled, dry_run):
    monkeypatch.setattr(pd, 'load_config', lambda: lp.PacingConfig(enabled=not disabled))
    monkeypatch.setattr(pd, 'read_pool', lambda *a: pytest.fail('unexpected usage read'))
    with kbc.connect() as conn:
        dispatch.dispatch_once(conn, max_spawn=0, dry_run=dry_run)
    assert not pd.state_path().exists()


def test_periodic_usage_failure_is_unknown_not_stale_pacing(home, monkeypatch):
    now = time.time()
    pd._save_state(pd.state_path(), {'anthropic': {'read_at': now - 1000,
        'pace': lp.PoolPace('anthropic', lp.PACED, 0.2).as_dict()}})
    monkeypatch.setattr(pd, 'load_config', lambda: lp.PacingConfig(providers=('anthropic',)))

    def failed(*args):
        raise OSError('usage unavailable')

    monkeypatch.setattr(pd, 'read_pool', failed)
    with kbc.connect() as conn:
        result = dispatch.dispatch_once(conn, max_spawn=0)
    assert not result.spawned
    state = pd._load_state(pd.state_path())
    stored = state['anthropic']
    assert time.time() - stored['read_at'] < 60
    assert lp.PoolPace.from_dict(stored['pace']).state == lp.UNKNOWN
    pool = state['v2']['pools']['anthropic']  # no reading: not at reserve, no planned rate (fail open)
    assert pool['at_reserve'] is False and pool['rate_tokens_per_s'] is None


def test_remote_only_ready_work_gets_fresh_read_only_notice(hub, monkeypatch):
    clock = [1800000000.0]
    monkeypatch.setattr(pd.time, 'time', lambda: clock[0])
    monkeypatch.setattr(pd, 'load_config', lambda: lp.PacingConfig(providers=('anthropic',)))
    reads = []

    def read(provider, now):
        reads.append(now)
        return [lp.Reading(now, 'account', lp.SEVEN_DAY, 100, now + 86400)]

    monkeypatch.setattr(pd, 'read_pool', read)
    with kbc.connect() as conn:
        tid = notices._pool_card(conn)
        for offset in (0, 300, 600, 900, 1200):
            clock[0] = 1800000000.0 + offset
            result = dispatch.dispatch_once(conn, max_spawn=0)
            assert not result.spawned and not result.scheduling_admitted
            before = pd.state_path().read_bytes()
            paces = pd.paced_pools(conn, tid)
            assert paces and paces[0].state == lp.HELD
            assert pd.state_path().read_bytes() == before
            assert conn.execute('SELECT status FROM tasks WHERE id = ?', (tid,)).fetchone()[0] == 'ready'
        assert len(reads) == 5
        # No producer ticks: the unchanged read-only reader must reject the old data.
        clock[0] += 900
        assert pd.paced_pools(conn, tid) is None
        assert pd.state_path().read_bytes() == before
        assert len(reads) == 5


@pytest.mark.parametrize('persist', [True, False])
def test_failed_reads_share_bounded_budget(home, monkeypatch, persist):
    clock = [1800000000.0]
    monkeypatch.setattr(pd.time, 'time', lambda: clock[0])
    cfg = lp.PacingConfig(providers=('anthropic',))
    monkeypatch.setattr(pd, 'load_config', lambda: cfg)
    reads = []

    def failed(provider, now):
        reads.append(now)
        raise OSError('usage unavailable')

    monkeypatch.setattr(pd, 'read_pool', failed)
    if not persist:
        monkeypatch.setattr(pd, '_save_state', lambda *a: None)
    with kbc.connect() as conn:
        for offset in (0, 60, 299, 300, 301):
            clock[0] = 1800000000.0 + offset
            if persist:
                pd._CACHE.clear()  # another dispatcher uses the persisted usage budget
            dispatch.dispatch_once(conn, max_spawn=0)
            assert pd.pace_for(conn, 'anthropic', cfg, clock[0]).state == lp.UNKNOWN
    assert reads == [1800000000.0, 1800000300.0]
