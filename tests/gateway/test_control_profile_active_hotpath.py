"""Active neighbours must not rebuild a stable thread's histogram each tick."""
import inspect

import pytest

import gateway.control_profile as profiler
from tests.gateway.test_control_profile_fastpath import FakeCode, FakeFrame, FakeThread, Harness


class CountedHistogram(dict):
    def __init__(self):
        super().__init__()
        self.writes = 0

    def __setitem__(self, key, value):
        self.writes += 1
        super().__setitem__(key, value)


def test_active_neighbour_keeps_stable_histogram_run_open(monkeypatch):
    stable, active = FakeThread(1, 11, 'stable'), FakeThread(2, 12, 'active')
    leaf = FakeFrame(FakeCode('stable_wait'))
    branches = [FakeFrame(FakeCode(name)) for name in ('helper_a', 'helper_b')]
    histograms = {}
    original = profiler._new_row

    def new_row(thread, native_id, sample):
        row = original(thread, native_id, sample)
        row['stack_counts'] = histograms[thread.name] = CountedHistogram()
        return row

    monkeypatch.setattr(profiler, '_new_row', new_row)
    Harness(monkeypatch, lambda now: {
        1: (stable, leaf), 2: (active, branches[round(now * 100) % 2]),
    })
    result = profiler._collect(1, 100, 10)
    rows = {row['name']: row for row in result['threads']}
    assert dict(rows['active']['top_self']) == {'fake.py:1:helper_a': 50, 'fake.py:1:helper_b': 50}
    assert dict(rows['stable']['top_self']) == {'fake.py:1:stable_wait': 100}
    assert all(row['samples'] == 100 and row['last_seen_sample'] == 99 for row in rows.values())
    assert leaf.walks == 1
    # Initial observation and final flush only, despite an active stack every tick.
    assert histograms['stable'].writes == 2


@pytest.mark.parametrize('flags', [inspect.CO_GENERATOR, inspect.CO_COROUTINE,
                                   inspect.CO_ASYNC_GENERATOR, inspect.CO_ITERABLE_COROUTINE])
def test_changed_leaf_entering_suspendable_chain_disables_reuse(monkeypatch, flags):
    stable, active = FakeThread(1, 11, 'stable'), FakeThread(2, 12, 'active')
    stable_frame = FakeFrame(FakeCode('stable'))
    ordinary = FakeFrame(FakeCode('ordinary'))
    callers = [FakeFrame(FakeCode('caller_a')), FakeFrame(FakeCode('caller_b'))]
    harness = None
    def caller():
        assert harness is not None
        return callers[0 if harness.now < 0.5 - 1e-9 else 1]

    suspendable = FakeFrame(FakeCode('suspendable', flags), caller)
    harness = Harness(monkeypatch, lambda now: {
        1: (stable, stable_frame),
        2: (active, ordinary if now < 0.2 - 1e-9 else suspendable),
    })
    rows = {row['name']: row for row in profiler._collect(1, 100, 10)['threads']}
    counts = dict(rows['active']['top_inclusive'])
    assert counts['fake.py:1:ordinary'] == 20
    assert counts['fake.py:1:caller_a'] == 30
    assert counts['fake.py:1:caller_b'] == 50
    assert rows['stable']['samples'] == rows['active']['samples'] == 100


def test_active_runs_flush_before_thread_identity_replacement(monkeypatch):
    old, new = FakeThread(1, 11, 'old'), FakeThread(1, 12, 'new')
    active = FakeThread(2, 22, 'active')
    same_frame = FakeFrame(FakeCode('wait'))
    branches = [FakeFrame(FakeCode(name)) for name in ('helper_a', 'helper_b')]
    Harness(monkeypatch, lambda now: {
        1: (old if now < 0.5 - 1e-9 else new, same_frame),
        2: (active, branches[round(now * 100) % 2]),
    })
    rows = {row['name']: row for row in profiler._collect(1, 100, 10)['threads']}
    assert rows['old']['samples'] == rows['new']['samples'] == 50
    assert rows['old']['last_seen_sample'] == 49
    assert rows['new']['first_seen_sample'] == 50
    assert rows['active']['samples'] == 100


def test_greenlet_appearing_during_active_runs_disables_reuse(monkeypatch):
    modules = {'other': object()}
    stable, active = FakeThread(1, 11, 'stable'), FakeThread(2, 22, 'active')
    callers = [FakeFrame(FakeCode('caller_a')), FakeFrame(FakeCode('caller_b'))]
    branches = [FakeFrame(FakeCode(name)) for name in ('helper_a', 'helper_b')]
    harness = None

    def caller():
        assert harness is not None
        return callers[0 if harness.now < 0.7 - 1e-9 else 1]

    leaf = FakeFrame(FakeCode('leaf'), caller)

    def state(now):
        if now >= 0.5 - 1e-9:
            modules['greenlet'] = object()
        return {1: (stable, leaf), 2: (active, branches[round(now * 100) % 2])}

    harness = Harness(monkeypatch, state, modules=modules)
    rows = {row['name']: row for row in profiler._collect(1, 100, 10)['threads']}
    counts = dict(rows['stable']['top_inclusive'])
    assert counts['fake.py:1:caller_a'] == 70
    assert counts['fake.py:1:caller_b'] == 30
    assert rows['stable']['samples'] == rows['active']['samples'] == 100
