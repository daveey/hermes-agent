"""Occupancy telemetry from the real dispatcher candidate filter, not invented states."""
import copy
import sqlite3

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


# --- Claim-time rejections through the real dispatcher -------------------------------------------------


def _capped(configured, cap):
    mapping = copy.deepcopy(base.LANES)
    mapping['babybear']['max_workers'] = cap
    configured(mapping)
    return lanes.policy()


def _dispatch(conn, *, max_spawn=3):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    return dispatch_once(conn, spawn_fn=lambda *_a, **_k: 4242, max_spawn=max_spawn, max_in_progress=10,
                         reconcile_orphans=False)


def _sample(conn, lane='babybear', ts=None):
    sql = "SELECT * FROM lane_samples WHERE lane=?" + ("" if ts is None else " AND ts=?")
    return conn.execute(sql, (lane,) if ts is None else (lane, ts)).fetchone()


@pytest.mark.parametrize('instrumented', [True, False])
def test_dispatch_once_claim_time_rejections_are_witnessed(conn, configured, monkeypatch, instrumented):
    """cap 1, census 0 at pass start, 3 ready: claims [T,F,F], at_capacity [F,T,T], prepass drops nothing."""
    import contextlib
    _capped(configured, 1)
    ready = [base._ready_in(conn, 'babybear', f'waiting {i}', priority=10 - i) for i in range(3)]
    if not instrumented:
        monkeypatch.setattr(samples, 'observe', lambda *_a, **_k: contextlib.nullcontext())
    result = _dispatch(conn)
    # Admission is identical with and without instrumentation.
    assert len(result.spawned) == 1, {k: v for k, v in vars(result).items() if v}
    statuses = [conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()[0] for t in ready]
    assert sorted(statuses) == ['ready', 'ready', 'running']
    if not instrumented:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lane_samples'").fetchone()
        return
    row = _sample(conn)
    assert (row['running'], row['cap'], row['rejected']) == (1, 1, 2)
    tick = samples.ticks(conn, lanes.policy(), now=row['ts'] + 30, window_s=60, count=1)['babybear'][0]
    assert (tick['state'], tick['running'], tick['cap'], tick['rejected']) == ('rejecting', 1, 1, 2)


def test_dispatch_once_uncapped_lane_is_still_censused(conn, configured):
    _capped(configured, None)
    assert kb.claim_task(conn, base._ready_in(conn, 'babybear', 'running'))
    base._ready_in(conn, 'babybear', 'waiting')
    _dispatch(conn, max_spawn=0)
    row = _sample(conn)
    assert (row['running'], row['cap'], row['rejected']) == (1, None, 0)
    assert samples.ticks(conn, lanes.policy(), now=row['ts'] + 30, window_s=60, count=1)['babybear'][0]['state'] == 'partial'


def test_prepass_and_claim_refusals_dedup_per_pass(conn, configured):
    from hermes_cli import kanban_lane_workers as workers
    _capped(configured, 1)
    assert kb.claim_task(conn, base._ready_in(conn, 'babybear', 'running'))
    waiting = base._ready_in(conn, 'babybear', 'waiting')
    with samples.observe(conn):
        assert lane_rows(conn, 'ready') == []        # prepass refusal
        assert lane_rows(conn, 'ready') == []        # repeated prepass
        assert workers.at_capacity(conn, waiting)    # claim-time refusal of the same card
        assert workers.at_capacity(conn, waiting)
    assert _sample(conn)['rejected'] == 1


def test_claim_refusal_outside_a_pass_records_nothing(conn, configured):
    from hermes_cli import kanban_lane_workers as workers
    _capped(configured, 1)
    assert kb.claim_task(conn, base._ready_in(conn, 'babybear', 'running'))
    assert workers.at_capacity(conn, base._ready_in(conn, 'babybear', 'waiting'))
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lane_samples'").fetchone()


@pytest.mark.parametrize('error', [sqlite3.OperationalError('locked'), OSError('disk')])
def test_record_failure_never_changes_dispatch(conn, configured, monkeypatch, caplog, error):
    _capped(configured, 1)
    ready = [base._ready_in(conn, 'babybear', f'waiting {i}') for i in range(3)]
    monkeypatch.setattr(samples, 'record', lambda *_a, **_k: (_ for _ in ()).throw(error))
    result = _dispatch(conn)
    assert len(result.spawned) == 1, {k: v for k, v in vars(result).items() if v}
    assert sorted(conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()[0] for t in ready) == [
        'ready', 'ready', 'running']
    assert 'lane occupancy sample unavailable' in caplog.text


def test_census_failure_is_contained_and_dispatch_still_runs(conn, configured, monkeypatch, caplog):
    _capped(configured, 1)
    ran = []

    def boom(*_a, **_k):
        raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(lanes, '_running', boom)
    with samples.observe(conn):
        ran.append(True)
    assert ran == [True]
    assert 'lane occupancy census unavailable' in caplog.text
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lane_samples'").fetchone()


def test_rejection_capture_is_advisory_and_skips_removed_lanes(conn, configured):
    _capped(configured, 1)
    with samples.observe(conn):
        samples.rejected(['t'], {'t': 'removed-lane'}, 'removed-lane', 5)   # lane left the policy: no KeyError
        samples.rejected(['t'], None, 'babybear', 5)                         # malformed: swallowed, not raised
        samples.rejected_at_claim('removed-lane', 't', 5)
    assert _sample(conn)['rejected'] == 0
    assert not _sample(conn, 'removed-lane')


# --- One advisory write unit ---------------------------------------------------------------------------


def test_record_nests_in_callers_transaction_without_taking_authority(conn, configured):
    pol = _capped(configured, 1)
    conn.execute("CREATE TABLE IF NOT EXISTS caller_marker (x)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO caller_marker VALUES (1)")
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 1), {n: set() for n in pol.lanes}, now=60)
    assert conn.in_transaction                    # not committed out from under the caller
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM caller_marker").fetchone()[0] == 0
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lane_samples'").fetchone()
    conn.execute("BEGIN IMMEDIATE")
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 1), {n: set() for n in pol.lanes}, now=60)
    conn.commit()
    assert _sample(conn)['running'] == 1


def test_record_standalone_commits_and_failure_rolls_back_whole_unit(conn, configured, monkeypatch):
    pol = _capped(configured, 1)
    empty = {n: set() for n in pol.lanes}
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 1), empty, now=60)
    assert not conn.in_transaction
    monkeypatch.setattr(samples, 'UPSERT', 'INSERT INTO nowhere VALUES (?,?,?,?,?,?)')
    with pytest.raises(sqlite3.Error):
        samples.record(conn, pol, dict.fromkeys(pol.lanes, 9), empty, now=7 * 60 + samples.RETENTION)
    assert not conn.in_transaction
    assert [r['running'] for r in conn.execute("SELECT running FROM lane_samples WHERE lane='babybear'")] == [1]


def test_retention_delete_uses_the_ts_index(conn, configured):
    pol = _capped(configured, 1)
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 0), {n: set() for n in pol.lanes}, now=60)
    plan = ' '.join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN DELETE FROM lane_samples WHERE ts < ?", (1,)))
    assert 'SEARCH' in plan and 'SCAN' not in plan


# --- Upsert semantics ----------------------------------------------------------------------------------


def _put(conn, configured, *, cap, running, rejected=0, now=120):
    pol = _capped(configured, cap)
    counts = dict.fromkeys(pol.lanes, 0)
    counts['babybear'] = running
    capture = {n: set() for n in pol.lanes}
    capture['babybear'] = {f'r{i}' for i in range(rejected)}
    samples.record(conn, pol, counts, capture, now=now)
    return pol


def _state(conn, ts=120):
    row = _sample(conn, ts=ts)
    return row['running'], row['cap'], row['rejected']


def test_new_rejection_overrides_a_busier_earlier_witness(conn, configured):
    _put(conn, configured, cap=3, running=2)
    _put(conn, configured, cap=1, running=1, rejected=1)
    assert _state(conn) == (1, 1, 1)


def test_recorded_rejection_witness_is_not_replaced_by_later_busier_sample(conn, configured):
    _put(conn, configured, cap=1, running=1, rejected=1)
    _put(conn, configured, cap=9, running=5)
    assert _state(conn) == (1, 1, 1)
    _put(conn, configured, cap=1, running=1, rejected=2)
    assert _state(conn) == (1, 1, 3)


def test_peak_occupancy_carries_its_latest_cap(conn, configured):
    _put(conn, configured, cap=2, running=1)
    _put(conn, configured, cap=3, running=2)
    assert _state(conn) == (2, 3, 0)
    _put(conn, configured, cap=9, running=1)      # a quieter later sample never lowers the peak
    assert _state(conn) == (2, 3, 0)
    _put(conn, configured, cap=4, running=2)      # equal peak: the latest cap
    assert _state(conn) == (2, 4, 0)


def test_ready_keeps_minute_maximum(conn, configured):
    for i in range(3):
        base._ready_in(conn, 'babybear', f'w{i}')
    _put(conn, configured, cap=2, running=0)
    assert _sample(conn, ts=120)['ready'] == 3
    conn.execute("UPDATE tasks SET status='done' WHERE id IN (SELECT id FROM tasks LIMIT 2)")
    _put(conn, configured, cap=2, running=0)
    assert _sample(conn, ts=120)['ready'] == 3
    for i in range(3):
        base._ready_in(conn, 'babybear', f'x{i}')
    _put(conn, configured, cap=2, running=0)
    assert _sample(conn, ts=120)['ready'] == 4


def test_ready_excludes_claim_locked_cards(conn, configured):
    base._ready_in(conn, 'babybear', 'free')
    locked = base._ready_in(conn, 'babybear', 'locked')
    conn.execute("UPDATE tasks SET claim_lock='held' WHERE id=?", (locked,))
    _put(conn, configured, cap=2, running=0)
    assert _sample(conn, ts=120)['ready'] == 1


def test_claim_witness_raises_recorded_running_to_refusal_level(conn, configured):
    pol = _capped(configured, 1)
    capture = samples.Capture(pol.lanes)
    capture['babybear'].add('x')
    capture.witness['babybear'] = 1
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 0), capture, now=120)
    assert _state(conn) == (1, 1, 1)
    capture = samples.Capture(pol.lanes)                     # no refusals: pass-start census stands
    capture.witness['babybear'] = 7
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 0), capture, now=180)
    assert _state(conn, 180) == (0, 1, 0)


# --- ticks() -------------------------------------------------------------------------------------------


def test_ticks_time_bounds(conn, configured):
    # start = 1000-300 = 700; its minute floor is 660.
    _put(conn, configured, cap=None, running=2, now=600)    # before the bucket floor: excluded
    _put(conn, configured, cap=None, running=1, now=660)    # exactly the floor: included
    tick = samples.ticks(conn, lanes.policy(), now=1000, window_s=300, count=5)['babybear'][0]
    assert (tick['state'], tick['running'], tick['rejected']) == ('partial', 1, 0)
    # now=960: a sample AT now counts, one a minute later does not.
    _put(conn, configured, cap=None, running=3, now=960)
    _put(conn, configured, cap=None, running=9, now=1020)
    last = samples.ticks(conn, lanes.policy(), now=960, window_s=300, count=5)['babybear'][-1]
    assert (last['state'], last['running']) == ('partial', 3)


def test_bucket_sums_rejections_across_minutes(conn, configured):
    _put(conn, configured, cap=1, running=1, rejected=1, now=120)
    _put(conn, configured, cap=1, running=1, rejected=2, now=180)
    tick = samples.ticks(conn, lanes.policy(), now=240, window_s=240, count=1)['babybear'][0]
    assert (tick['state'], tick['rejected']) == ('rejecting', 3)


def test_zero_cap_idle_lane_is_empty_not_full(conn, configured):
    _put(conn, configured, cap=0, running=0)
    tick = samples.ticks(conn, lanes.policy(), now=150, window_s=60, count=1)['babybear'][0]
    assert (tick['state'], tick['running'], tick['cap'], tick['rejected']) == ('empty', 0, 0, 0)


def test_equal_severity_prefers_busier_then_latest_minute(conn, configured):
    _put(conn, configured, cap=2, running=2, now=120)
    _put(conn, configured, cap=1, running=1, now=180)       # later but lower: both 'full'
    tick = samples.ticks(conn, lanes.policy(), now=240, window_s=240, count=1)['babybear'][0]
    assert (tick['state'], tick['running'], tick['cap']) == ('full', 2, 2)
    _put(conn, configured, cap=1, running=2, now=240)       # equal severity AND running: latest cap wins
    tick = samples.ticks(conn, lanes.policy(), now=250, window_s=300, count=1)['babybear'][0]
    assert (tick['state'], tick['running'], tick['cap']) == ('full', 2, 1)


def test_samples_for_a_removed_lane_are_ignored(conn, configured):
    _put(conn, configured, cap=1, running=1)
    conn.execute("INSERT INTO lane_samples VALUES (120,'removed',1,1,0,0)")
    out = samples.ticks(conn, lanes.policy(), now=150, window_s=60, count=1)
    assert 'removed' not in out and out['babybear'][0]['state'] == 'full'


def test_zero_length_and_inverted_run_spans_are_ignored_by_backfill(conn, configured):
    pol = lanes.policy()
    zero = base._ready_in(conn, 'babybear', 'zero')
    inverted = base._ready_in(conn, 'babybear', 'inverted')
    for tid, start, end in [(zero, 110, 110), (inverted, 130, 120)]:
        assert kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET started_at=?,ended_at=? WHERE task_id=?", (start, end, tid))
    samples.record(conn, pol, dict.fromkeys(pol.lanes, 0), {n: set() for n in pol.lanes}, now=240)
    ticks = samples.ticks(conn, pol, now=300, window_s=300, count=3)['babybear']
    assert ticks[1]['state'] == 'unknown' and ticks[1]['running'] is None


@pytest.fixture(autouse=True)
def _routable(monkeypatch):
    """Model routing is not under test: let every class selector reach the claim."""
    from hermes_cli import kanban_model_tier
    monkeypatch.setattr(kanban_model_tier, 'class_unroutable_reason', lambda *_a, **_k: None)


def test_failed_record_leaves_no_partial_schema_or_open_savepoint(conn, configured, monkeypatch):
    pol = _capped(configured, 1)
    monkeypatch.setattr(samples, 'UPSERT', 'INSERT INTO nowhere VALUES (?,?,?,?,?,?)')
    with pytest.raises(sqlite3.Error):
        samples.record(conn, pol, dict.fromkeys(pol.lanes, 1), {n: set() for n in pol.lanes}, now=60)
    assert not conn.in_transaction
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='lane_samples'").fetchone()
