"""Thread lifetime, recursive stack and native CPU contracts."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.control_profile import profile


def _recursive_wait(depth, ready, release):
    if depth:
        return _recursive_wait(depth - 1, ready, release)
    ready.set()
    release.wait(5)


@pytest.mark.platforms('linux', 'macos')
def test_short_lived_and_sleeping_threads_retained(monkeypatch):
    import gateway.control_profile as profiler
    ready, release = threading.Event(), threading.Event()
    sampled = threading.Event()
    started = threading.Event()
    reads = {}
    read_cpu = profiler._cpu_seconds

    def observed(tid):
        reads[tid] = reads.get(tid, 0) + 1
        started.set()
        if 'short' in locals_for_observer and tid == locals_for_observer['short'].native_id and reads[tid] >= 5:
            sampled.set()
        return read_cpu(tid)

    locals_for_observer = {}
    monkeypatch.setattr(profiler, '_cpu_seconds', observed)
    sleeper = threading.Thread(target=release.wait, name='sleeper')
    sleeper.start()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(profile, {'seconds': 2, 'hz': 100})
        assert started.wait(3)
        short = threading.Thread(target=_recursive_wait, args=(6, ready, release), name='short-lived')
        locals_for_observer['short'] = short
        short.start()
        assert ready.wait(3)
        assert sampled.wait(3)
        release.set()
        short.join(3)
        sleeper.join(3)
        result = future.result(5)
    rows = {t['name']: t for t in result['threads']}
    short_row = rows['short-lived']
    assert short_row['first_seen_sample'] > 0
    assert short_row['last_seen_sample'] < result['samples'] - 1
    assert short_row['cpu_partial'] is True
    code = _recursive_wait.__code__
    key = f'{code.co_filename}:{code.co_firstlineno}:{code.co_name}'
    assert dict(short_row['top_inclusive'])[key] <= short_row['samples']
    assert 0 <= rows['sleeper']['cpu_s'] < 0.05


@pytest.mark.platforms('linux', 'macos')
def test_same_name_successive_thread_lifetimes_remain_separate(monkeypatch):
    import gateway.control_profile as profiler
    from tests.gateway.test_control_profile import _profile_burner_spin
    read_cpu = profiler._cpu_seconds
    counts = {}
    stops = {}
    started = threading.Event()

    def observed(tid):
        value = read_cpu(tid)
        counts[tid] = counts.get(tid, 0) + 1
        started.set()
        if counts[tid] >= 8 and tid in stops:
            stops[tid].append(True)
        return value

    monkeypatch.setattr(profiler, '_cpu_seconds', observed)
    workers = []
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(profile, {'seconds': 2, 'hz': 100})
        assert started.wait(3)
        for _ in range(2):
            stop = []
            thread = threading.Thread(target=_profile_burner_spin, args=(stop,), name='reused-name', daemon=True)
            thread.start()
            workers.append(thread)
            stops[thread.native_id] = stop
            try:
                thread.join(3)
                assert not thread.is_alive()
            finally:
                stop.append(True)
        result = future.result(5)
    rows = [r for r in result['threads'] if r['name'] == 'reused-name']
    assert len(rows) == 2
    assert {r['native_id'] for r in rows} == {t.native_id for t in workers}
    assert all(r['cpu_s'] > 0 and r['cpu_partial'] for r in rows)
    rows.sort(key=lambda r: r['first_seen_sample'])
    assert rows[0]['last_seen_sample'] < rows[1]['first_seen_sample']


@pytest.mark.platforms('macos')
def test_macos_cpu_reader_tracks_current_thread():
    from gateway.control_profile import _cpu_seconds
    import time
    tid = threading.get_native_id()
    before = _cpu_seconds(tid)
    end = time.thread_time() + 0.02
    while time.thread_time() < end:
        pass
    after = _cpu_seconds(tid)
    assert before is not None and after is not None
    assert after > before


def test_darwin_reader_queries_thread_id64_flavor(monkeypatch):
    """Runs everywhere: pins the XNU ABI the macOS-only test above cannot check on Linux CI.

    threading.native_id on macOS is pthread_threadid_np()'s 64-bit kernel thread_id,
    which only PROC_PIDTHREADID64INFO (15) matches; PROC_PIDTHREADINFO (5) expects the
    PROC_PIDLISTTHREADS TSD handle and returns 0 bytes (ESRCH) for a thread_id.
    """
    import ctypes
    import types
    import gateway.control_profile as profiler
    calls = []

    def fake_proc_pidinfo(pid, flavor, arg, buffer, size):
        calls.append((pid, flavor, arg, size))
        if flavor != 15 or arg != 4242:
            return 0
        info = profiler._ProcThreadInfo.from_address(buffer)
        info.user_time, info.system_time = 1_500_000_000, 250_000_000
        return size

    monkeypatch.setattr(profiler, 'sys', types.SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(profiler, '_darwin_proc_pidinfo', lambda: fake_proc_pidinfo)
    assert ctypes.sizeof(profiler._ProcThreadInfo) == 112
    assert profiler._cpu_seconds(4242) == pytest.approx(1.75)
    assert profiler._cpu_seconds(4243) is None
    assert [c[1:] for c in calls] == [(15, 4242, 112), (15, 4243, 112)]


def test_darwin_reader_holds_gil_with_pydll(monkeypatch):
    """proc_pidinfo is a bounded non-blocking read: load it via PyDLL (GIL held),
    not CDLL, which releases/re-acquires the GIL on every per-thread call."""
    import ctypes
    import types
    import gateway.control_profile as profiler
    loaded = []

    def make(kind):
        def factory(path, *args, **kwargs):
            loaded.append((kind, path))
            return types.SimpleNamespace(proc_pidinfo=types.SimpleNamespace())
        return factory

    monkeypatch.setattr(ctypes, 'PyDLL', make('PyDLL'))
    monkeypatch.setattr(ctypes, 'CDLL', make('CDLL'))
    profiler._darwin_proc_pidinfo.cache_clear()
    try:
        profiler._darwin_proc_pidinfo()
    finally:
        profiler._darwin_proc_pidinfo.cache_clear()
    assert loaded == [('PyDLL', '/usr/lib/libproc.dylib')]
