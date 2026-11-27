"""Occupancy telemetry from the real dispatcher candidate filter, not invented states."""
import copy

import pytest

from hermes_cli import kanban_db as kb, kanban_lane_samples as samples, kanban_lanes as lanes
from hermes_cli.kanban_db_dispatch_queries import lane_rows
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


@pytest.mark.parametrize('running,cap,ready,state', [
    (0, 2, 0, 'empty'), (1, 2, 0, 'partial'), (1, 1, 0, 'full'),
    (1, 1, 1, 'rejecting'), (0, 0, 1, 'rejecting'), (1, None, 1, 'partial')])
def test_real_candidate_filter_observes_states(conn, configured, monkeypatch, running, cap, ready, state):
    mapping = copy.deepcopy(base.LANES)
    mapping['babybear']['max_workers'] = cap
    configured(mapping)
    monkeypatch.setattr(samples.time, 'time', lambda: 12000)
    for i in range(running):
        tid = base._ready_in(conn, 'babybear', f'running {i}')
        assert kb.claim_task(conn, tid)
    for i in range(ready):
        base._ready_in(conn, 'babybear', f'ready {i}')
    with samples.observe(conn):
        rows = lane_rows(conn, 'ready')
    observed = conn.execute("SELECT * FROM lane_samples WHERE lane='babybear'").fetchone()
    assert observed['running'] == running
    assert observed['cap'] == cap
    assert observed['rejected'] == (ready if state == 'rejecting' else 0)
    tick = samples.ticks(conn, lanes.policy(), now=12030, window_s=60, count=1)['babybear'][0]
    assert tick['state'] == state
    assert len(rows) == (0 if state == 'rejecting' else ready)


@pytest.mark.parametrize('running,cap,ready,state', [
    (0, 2, 0, 'empty'), (1, 2, 0, 'partial'), (1, 1, 0, 'full'), (1, 1, 1, 'rejecting')])
def test_dispatch_once_records_states(conn, configured, running, cap, ready, state):
    mapping = copy.deepcopy(base.LANES)
    mapping['babybear']['max_workers'] = cap
    configured(mapping)
    for i in range(running):
        first = base._ready_in(conn, 'babybear', f'running {i}')
        assert kb.claim_task(conn, first)
    for i in range(ready):
        base._ready_in(conn, 'babybear', f'waiting {i}')
    from hermes_cli.kanban_db_dispatch import dispatch_once
    result = dispatch_once(conn, spawn_fn=lambda *_a, **_k: pytest.fail('cap must prevent spawn'),
                           max_spawn=2, max_in_progress=10, reconcile_orphans=False)
    assert not result.spawned
    row = conn.execute("SELECT * FROM lane_samples WHERE lane='babybear'").fetchone()
    assert row['running'] == running
    assert row['rejected'] == ready
    tick = samples.ticks(conn, lanes.policy(), now=row['ts'] + 30, window_s=60, count=1)['babybear'][0]
    assert tick['state'] == state


def test_reads_and_dry_runs_never_write(conn, configured):
    with samples.observe(conn, dry_run=True):
        lane_rows(conn, 'ready')
    assert samples.ticks(conn, lanes.policy()) is None
    lane_rows(conn, 'ready')
    assert samples.ticks(conn, lanes.policy()) is None


def test_minute_aggregation_retention_and_missing(conn, configured):
    pol = lanes.policy()
    counts = dict.fromkeys(pol.lanes, 0)
    capture = {name: set() for name in pol.lanes}
    samples.record(conn, pol, counts, capture, now=0)
    capture['babybear'].add('first')
    samples.record(conn, pol, counts, capture, now=samples.RETENTION + 120)
    capture['babybear'].add('second')
    samples.record(conn, pol, counts, capture, now=samples.RETENTION + 130)
    rows = conn.execute("SELECT * FROM lane_samples WHERE lane='babybear'").fetchall()
    assert len(rows) == 1
    assert rows[0]['rejected'] == 3
    ticks = samples.ticks(conn, pol, now=samples.RETENTION + 140, window_s=240, count=4)['babybear']
    assert ticks[0]['state'] == 'unknown'
    assert ticks[0]['running'] is None
    assert ticks[-1]['state'] == 'empty'


def test_rejection_survives_drain_in_same_minute(conn, configured):
    mapping = copy.deepcopy(base.LANES)
    mapping['babybear']['max_workers'] = 1
    configured(mapping)
    pol = lanes.policy()
    counts = dict.fromkeys(pol.lanes, 0)
    counts['babybear'] = 1
    capture = {n: set() for n in pol.lanes}
    capture['babybear'].add('waiting')
    samples.record(conn, pol, counts, capture, now=120)
    counts['babybear'] = 0
    capture['babybear'].clear()
    samples.record(conn, pol, counts, capture, now=130)
    tick = samples.ticks(conn, pol, now=150, window_s=60, count=1)['babybear'][0]
    assert tick['state'] == 'rejecting'
    assert (tick['running'], tick['cap'], tick['rejected']) == (1, 1, 1)


def test_shadow_and_unconfigured_do_not_write(conn, configured):
    configured(lane_shadow=True)
    with samples.observe(conn):
        lane_rows(conn, 'ready')
    assert samples.ticks(conn, lanes.policy()) is None
    configured({})
    with samples.observe(conn):
        pass
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lane_samples'").fetchone()


def test_backfill_uses_peak_not_sum_and_keeps_rejections_unknown(conn, configured):
    first = base._ready_in(conn, 'babybear', 'first')
    second = base._ready_in(conn, 'babybear', 'second')
    for tid, start, end in [(first, 100, 110), (second, 110, 120)]:
        assert kb.claim_task(conn, tid)
        conn.execute('UPDATE tasks SET status=\'done\' WHERE id=?', (tid,))
        conn.execute('UPDATE task_runs SET started_at=?,ended_at=? WHERE task_id=?', (start, end, tid))
    pol = lanes.policy()
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 0), {n: set() for n in pol.lanes}, now=240)
    ticks = samples.ticks(conn, pol, now=300, window_s=300, count=3)['babybear']
    assert ticks[0]['state'] == 'unknown'
    assert ticks[1] == dict(t=100, state='partial', running=1, cap=None, rejected=None, rejection_unknown=True)
    assert ticks[2]['state'] == 'empty'


@pytest.mark.parametrize('window,count', [(0, 48), (9999999, 48), (60, 0), (60, 337)])
def test_invalid_window(conn, configured, window, count):
    with pytest.raises(ValueError):
        samples.ticks(conn, lanes.policy(), window_s=window, count=count)
