"""Deterministic sampler contracts: snapshot fast path, CPU cadence, lifetimes.

A fake clock, frame snapshot and thread registry drive ``_collect`` so exact
counts and read schedules can be asserted without timing flakiness.
"""
import gc
import inspect
from itertools import pairwise
import types
import weakref

import pytest

import gateway.control_profile as profiler


class FakeCode:
    def __init__(self, name, flags=0):
        self.co_filename, self.co_firstlineno, self.co_name, self.co_flags = 'fake.py', 1, name, flags

    @property
    def key(self):
        return f'fake.py:1:{self.co_name}'


class FakeFrame:
    """Weak-referenceable stand-in; ``back`` may be a callable for a changing caller."""

    def __init__(self, code, back=None):
        self.f_code, self._back, self.walks = code, back, 0

    @property
    def f_back(self):
        self.walks += 1
        return self._back() if callable(self._back) else self._back


class FakeThread:
    def __init__(self, ident, native_id, name):
        self.ident, self.native_id, self.name, self.alive = ident, native_id, name, True

    def is_alive(self):
        return self.alive


class Harness:
    """Fake ``time``/``sys``/``threading`` for ``_collect``.

    ``state(now)`` returns ``{ident: (thread, frame)}`` for the snapshot at ``now``.
    ``registry(now, call_in_tick)`` may override what ``threading.enumerate`` sees.
    """

    def __init__(self, monkeypatch, state, cpu=None, registry=None, modules=None, jumps=None):
        self.now = 0.0
        self.state = state
        self.registry = registry
        self.jumps = dict(jumps or {})
        self.frames_calls = self.enumerate_calls = 0
        self.reads = []
        self._tick_enumerates = (None, 0)
        self.cpu = cpu or (lambda tid, now: now)
        clock = types.SimpleNamespace(monotonic=lambda: self.now, sleep=self._sleep)
        fake_sys = types.SimpleNamespace(_current_frames=self._frames, modules=modules or {})
        fake_threading = types.SimpleNamespace(enumerate=self._enumerate)
        monkeypatch.setattr(profiler, 'time', clock)
        monkeypatch.setattr(profiler, 'sys', fake_sys)
        monkeypatch.setattr(profiler, 'threading', fake_threading)
        monkeypatch.setattr(profiler, '_cpu_seconds', self._read)

    def _sleep(self, seconds):
        self.now += seconds
        for at, extra in list(self.jumps.items()):
            if self.now >= at:
                self.now += extra
                del self.jumps[at]

    def _frames(self):
        self.frames_calls += 1
        return {ident: frame for ident, (_, frame) in self.state(self.now).items()}

    def _enumerate(self):
        self.enumerate_calls += 1
        at, count = self._tick_enumerates
        count = count + 1 if at == self.now else 1
        self._tick_enumerates = (self.now, count)
        if self.registry is not None:
            return self.registry(self.now, count)
        return [thread for thread, _ in self.state(self.now).values()]

    def _read(self, tid):
        self.reads.append((tid, self.now))
        return self.cpu(tid, self.now)


def _rows(result):
    return {row['name']: row for row in result['threads']}


def test_identical_safe_snapshots_batch_exact_counts_and_skip_per_thread_work(monkeypatch):
    leaf, mid, root = FakeCode('leaf'), FakeCode('mid'), FakeCode('root')
    recursive_root = FakeFrame(root)
    # root -> mid -> mid -> leaf: recursive mid must count once per inclusive sample.
    chains = {i: FakeFrame(leaf, FakeFrame(mid, FakeFrame(mid, recursive_root))) for i in (1, 2, 3)}
    threads = {i: FakeThread(i, 100 + i, f'idle-{i}') for i in (1, 2, 3)}
    h = Harness(monkeypatch, lambda now: {i: (threads[i], chains[i]) for i in threads})
    result = profiler._collect(1, 100, 10)

    assert result['samples'] == 100
    assert result['cpu_refresh_s'] == pytest.approx(0.1)
    for row in result['threads']:
        assert row['samples'] == 100
        assert row['first_seen_sample'] == 0 and row['last_seen_sample'] == 99
        assert dict(row['top_self']) == {leaf.key: 100}
        assert dict(row['top_inclusive']) == {leaf.key: 100, mid.key: 100, root.key: 100}
        assert row['cpu_partial'] is False
    # Each chain is walked once, then reused by frame identity.
    assert all(frame.walks == 1 for frame in chains.values())
    # Per-thread work (registry sandwich + CPU reads) only on CPU-refresh ticks.
    read_times = sorted({now for _, now in h.reads})
    cadence_ticks = len(read_times) - 1  # minus the end-of-window read
    assert 9 <= cadence_ticks <= 11
    assert h.enumerate_calls == 2 * cadence_ticks
    assert len(h.reads) == 3 * len(read_times)


def test_cpu_refresh_keeps_stable_snapshot_run_open(monkeypatch):
    thread = FakeThread(1, 11, 'stable')
    frame = FakeFrame(FakeCode('wait'))
    Harness(monkeypatch, lambda now: {1: (thread, frame)})
    flushes = []
    original = profiler._Window.flush

    def flush(window):
        flushes.append(window.run)
        return original(window)

    monkeypatch.setattr(profiler._Window, 'flush', flush)
    row = _rows(profiler._collect(1, 100, 5))['stable']
    assert row['samples'] == 100
    assert row['cpu_s'] == pytest.approx(1.0)
    # Initial walk and final flush only; CPU refresh must not redo histogram work.
    assert flushes == [0, 99]


def test_refresh_registry_change_does_not_reuse_old_lifetime(monkeypatch):
    old, new = FakeThread(1, 11, 'old'), FakeThread(1, 12, 'new')
    frame = FakeFrame(FakeCode('wait'))
    # Deliberately identical snapshot, but different owner at a CPU-refresh tick.
    Harness(monkeypatch, lambda now: {1: (old if now < 0.1 else new, frame)})
    rows = _rows(profiler._collect(1, 100, 5))
    assert set(rows) == {'old', 'new'}
    assert sum(row['samples'] for row in rows.values()) == 100
    assert rows['old']['last_seen_sample'] < rows['new']['first_seen_sample']


def test_refresh_registry_sandwich_checks_before_as_well_as_after(monkeypatch):
    thread, other = FakeThread(1, 11, 'stable'), FakeThread(1, 12, 'other')
    frame = FakeFrame(FakeCode('wait'))

    def registry(now, call_in_tick):
        if 0.105 < now < 0.115 and call_in_tick == 1:
            return [other]
        return [thread]

    Harness(monkeypatch, lambda now: {1: (thread, frame)}, registry=registry)
    row = _rows(profiler._collect(1, 100, 5))['stable']
    assert row['samples'] == 99  # ambiguous sandwich tick is not attributed
    assert dict(row['top_self']) == {'fake.py:1:wait': 99}
    assert dict(row['top_inclusive']) == {'fake.py:1:wait': 99}


def test_ancestor_generator_caller_change_is_rewalked(monkeypatch):
    leaf = FakeCode('leaf')
    gen = FakeCode('gen', inspect.CO_GENERATOR)
    first, second = FakeFrame(FakeCode('caller_a')), FakeFrame(FakeCode('caller_b'))
    h = None
    # Same leaf object and constant CPU; only the suspended generator's caller changes.
    gen_frame = FakeFrame(gen, lambda: first if h.now < 0.5 - 1e-9 else second)
    leaf_frame = FakeFrame(leaf, gen_frame)
    thread = FakeThread(1, 11, 'gen-thread')
    h = Harness(monkeypatch, lambda now: {1: (thread, leaf_frame)}, cpu=lambda tid, now: 1.0)
    row = _rows(profiler._collect(1, 100, 10))['gen-thread']
    inclusive = dict(row['top_inclusive'])
    assert inclusive['fake.py:1:caller_a'] + inclusive['fake.py:1:caller_b'] == row['samples'] == 100
    assert inclusive['fake.py:1:caller_a'] == 50
    assert inclusive[leaf.key] == inclusive[gen.key] == 100


def test_greenlet_present_forces_rewalk_of_plain_chain(monkeypatch):
    leaf = FakeCode('leaf')
    first, second = FakeFrame(FakeCode('caller_a')), FakeFrame(FakeCode('caller_b'))
    h = None
    leaf_frame = FakeFrame(leaf, lambda: first if h.now < 0.5 - 1e-9 else second)
    thread = FakeThread(1, 11, 'greenlet-thread')
    h = Harness(monkeypatch, lambda now: {1: (thread, leaf_frame)}, cpu=lambda tid, now: 1.0,
                modules={'greenlet': object()})
    inclusive = dict(_rows(profiler._collect(1, 100, 10))['greenlet-thread']['top_inclusive'])
    assert inclusive['fake.py:1:caller_a'] == 50 and inclusive['fake.py:1:caller_b'] == 50


def test_cpu_cadence_without_catchup_and_exit_lower_bound(monkeypatch):
    code = FakeCode('wait')
    stays, leaves = FakeThread(1, 11, 'stays'), FakeThread(2, 12, 'leaves')
    frame_a, frame_b = FakeFrame(code), FakeFrame(code)

    def state(now):
        alive = {1: (stays, frame_a)}
        if now < 0.555:
            alive[2] = (leaves, frame_b)
        return alive

    def cpu(tid, now):
        if tid == 12 and now >= 0.53:
            # Thread is exiting: a Linux TID could already alias another thread.
            leaves.alive = False
            return 1000.0
        return now

    # One 0.35 s scheduling stall must not cause back-to-back CPU-refresh ticks.
    h = Harness(monkeypatch, state, cpu=cpu, jumps={0.2: 0.35})
    result = profiler._collect(1, 100, 10)
    rows = _rows(result)
    stays_reads = [now for tid, now in h.reads if tid == 11]
    gaps = [b - a for a, b in pairwise(stays_reads[:-1])]  # end-of-window read excluded
    assert gaps and min(gaps) >= 0.1 - 1e-9
    assert max(gaps) <= 0.35 + 0.1 + 0.02
    assert rows['stays']['cpu_partial'] is False
    assert rows['stays']['cpu_last_sample'] == result['samples']
    assert rows['stays']['cpu_s'] == pytest.approx(stays_reads[-1] - stays_reads[0])

    leaves_row = rows['leaves']
    good = [now for tid, now in h.reads if tid == 12 and now < 0.53]
    assert leaves_row['cpu_partial'] is True
    assert leaves_row['cpu_s'] == pytest.approx(good[-1] - good[0])  # dead read discarded
    assert leaves_row['cpu_last_sample'] <= leaves_row['last_seen_sample'] < result['samples'] - 1


def test_failed_first_cpu_read_recovers_but_is_partial(monkeypatch):
    thread = FakeThread(1, 11, 'late-cpu')
    frame = FakeFrame(FakeCode('wait'))
    seen = []

    def cpu(tid, now):
        seen.append(now)
        return None if len(seen) == 1 else now

    h = Harness(monkeypatch, lambda now: {1: (thread, frame)}, cpu=cpu)
    row = _rows(profiler._collect(1, 100, 10))['late-cpu']
    assert row['cpu_partial'] is True
    assert row['cpu_s'] == pytest.approx(seen[-1] - seen[1])
    assert h.reads


def test_enumerate_sandwich_rejects_reused_ident(monkeypatch):
    code = FakeCode('wait')
    old, new = FakeThread(7, 70, 'old'), FakeThread(7, 71, 'new')
    old_frame, new_frame = FakeFrame(code), FakeFrame(code)
    boundary, after_boundary = 0.285, 0.295  # tick 29 lies between these

    def state(now):
        # The boundary tick's snapshot already shows the new lifetime's frame.
        return {7: (old, old_frame) if now < boundary else (new, new_frame)}

    def registry(now, call_in_tick):
        # On the boundary tick the ident is reused between the two enumerations.
        if now < boundary:
            return [old]
        if now < after_boundary:
            return [old] if call_in_tick == 1 else [new]
        return [new]

    h = Harness(monkeypatch, state, registry=registry)
    result = profiler._collect(1, 100, 10)
    rows = _rows(result)
    assert set(rows) == {'old', 'new'}
    assert rows['old']['samples'] + rows['new']['samples'] == result['samples'] - 1
    assert rows['old']['last_seen_sample'] + 2 == rows['new']['first_seen_sample']
    assert h.frames_calls >= result['samples']


def test_no_frame_references_survive_success_or_error(monkeypatch):
    code = FakeCode('wait')
    thread = FakeThread(1, 11, 't')
    holder = {'frame': FakeFrame(code, FakeFrame(FakeCode('root')))}
    ref = weakref.ref(holder['frame'])
    Harness(monkeypatch, lambda now: {1: (thread, holder['frame'])} if 'frame' in holder else {})
    profiler._collect(1, 100, 10)
    del holder['frame']
    gc.collect()
    assert ref() is None

    holder['frame'] = FakeFrame(code)
    ref = weakref.ref(holder['frame'])
    calls = []

    def failing(tid, now):
        calls.append(now)
        if len(calls) == 3:
            raise RuntimeError('reader failed')
        return now

    h = Harness(monkeypatch, lambda now: {1: (thread, holder['frame'])} if 'frame' in holder else {}, cpu=failing)
    with pytest.raises(RuntimeError) as caught:
        profiler._collect(1, 100, 10)
    del holder['frame']
    gc.collect()
    assert caught.value.__traceback__ is not None
    assert ref() is None
    assert h.reads


@pytest.mark.platforms('linux', 'macos')
def test_real_idle_threads_take_batched_ticks(monkeypatch):
    """Real frames: the sampler's own frame must be identical across snapshots."""
    import threading
    real_enumerate = threading.enumerate
    calls = []

    def counted():
        calls.append(1)
        return real_enumerate()

    monkeypatch.setattr(profiler, 'threading', types.SimpleNamespace(enumerate=counted))
    stop = threading.Event()
    idle = [threading.Thread(target=stop.wait, name=f'idle-{i}', daemon=True) for i in range(20)]
    for thread in idle:
        thread.start()
    try:
        result = profiler._collect(1, 100, 5)
    finally:
        stop.set()
        for thread in idle:
            thread.join(3)
    rows = [r for r in result['threads'] if r['name'].startswith('idle-')]
    assert len(rows) == 20
    assert all(r['samples'] == result['samples'] for r in rows)
    # ~11 CPU-refresh ticks plus slack for unrelated thread churn; unbatched would be ~200.
    assert len(calls) <= 2 * 30, len(calls)
