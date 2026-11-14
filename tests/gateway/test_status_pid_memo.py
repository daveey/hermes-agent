"""Memoized ``get_running_pid`` for polled per-profile status (design t_01a1d563324d7e8e §3, card J). [daveey]

Gate: the PID/lock files' identity, the cached process staying alive under its start time with its lock held,
and a ``RECHECK_S`` backstop. RED before the memo: ``_check_gateway_running`` re-ran the full probe every call.
"""

from __future__ import annotations

import os
import time

import pytest

from gateway import status
from gateway import status_pid_memo as memo


@pytest.fixture(autouse=True)
def _fresh():
    memo.clear()
    yield
    memo.clear()


@pytest.fixture
def files(tmp_path):
    pid_path = tmp_path / "gateway.pid"
    pid_path.write_text("{}")
    status._get_gateway_lock_path(pid_path).write_text("")
    return pid_path


class _Probe:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def __call__(self, path):
        self.calls += 1
        return self.answer


@pytest.fixture(params=["procfs", "portable"])
def alive(request, monkeypatch):
    """Drive both liveness paths: Linux's one /proc read, and kill(0)+start time+lock elsewhere."""
    state = {"alive": True, "start": 111, "locked": True, "zombie": False}

    def _proc(pid):
        if request.param != "procfs":
            return None
        if not state["alive"]:
            return b"", -1
        return (b"Z" if state["zombie"] else b"S"), state["start"]

    monkeypatch.setattr(memo, "_proc_stat", _proc)
    monkeypatch.setattr(memo, "_pid_alive", lambda pid: state["alive"])
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: state["start"])
    monkeypatch.setattr(status, "is_gateway_runtime_lock_active",
                        lambda path=None: state["locked"] and not state["zombie"])
    return state


def test_unchanged_files_and_live_process_reuse_the_answer(files, alive):
    probe = _Probe(4242)
    assert [memo.running_pid(files, probe=probe, now=t) for t in (0, 1, 2, 3)] == [4242] * 4
    assert probe.calls == 1


def test_stopped_gateway_answer_is_reused_until_a_file_moves(files, alive):
    probe = _Probe(None)
    assert memo.running_pid(files, probe=probe, now=0) is None
    assert memo.running_pid(files, probe=probe, now=1) is None
    assert probe.calls == 1
    probe.answer = 777  # a gateway starts: it rewrites the PID file
    files.write_text('{"pid": 777, "started": true}')
    assert memo.running_pid(files, probe=probe, now=2) == 777
    assert probe.calls == 2


def test_lock_file_change_is_picked_up_next_call(files, alive):
    probe = _Probe(None)
    memo.running_pid(files, probe=probe, now=0)
    lock = status._get_gateway_lock_path(files)
    os.replace(_write(lock.with_name("new.lock"), "x"), lock)
    probe.answer = 9
    assert memo.running_pid(files, probe=probe, now=1) == 9


def _write(path, text):
    path.write_text(text)
    return path


@pytest.mark.parametrize("change", ["dead", "pid_reused", "zombie"])
def test_cached_live_pid_rechecked_without_any_file_change(files, alive, change):
    probe = _Probe(4242)
    assert memo.running_pid(files, probe=probe, now=0) == 4242
    if change == "dead":
        alive["alive"] = False
    elif change == "pid_reused":
        alive["start"] = 222
    else:
        alive["zombie"] = True
    probe.answer = None
    assert memo.running_pid(files, probe=probe, now=1) is None
    assert probe.calls == 2


def test_recheck_deadline_passing_reprobes_with_no_other_change(files, alive):
    probe = _Probe(None)
    memo.running_pid(files, probe=probe, now=0)
    memo.running_pid(files, probe=probe, now=memo.RECHECK_S - 0.01)
    assert probe.calls == 1
    probe.answer = 31
    assert memo.running_pid(files, probe=probe, now=memo.RECHECK_S) == 31
    assert probe.calls == 2


def test_file_moving_during_probe_is_not_memoized(files, alive):
    calls = []

    def racy(path):
        calls.append(1)
        if len(calls) == 1:
            files.write_text('{"pid": 5, "mid-probe": true}')
        return 5

    assert memo.running_pid(files, probe=racy, now=0) == 5
    assert memo.running_pid(files, probe=racy, now=1) == 5
    assert len(calls) == 2


def test_answers_are_not_shared_across_probes(files, alive):
    a, b = _Probe(1), _Probe(None)
    assert memo.running_pid(files, probe=a, now=0) == 1
    assert memo.running_pid(files, probe=b, now=0) is None
    assert (a.calls, b.calls) == (1, 1)


@pytest.mark.parametrize("distinct", ["paths", "probes"])
def test_memo_retention_is_bounded_under_key_churn(files, distinct):
    """Profile/probe churn must not retain unbounded paths or callable objects."""
    shared = _Probe(None)
    # Fixed workload and budget: changing the production cap must not resize this test.
    for n in range(600):
        path = files.with_name(f"profile-{n}.pid") if distinct == "paths" else files
        probe = _Probe(None) if distinct == "probes" else shared
        before = probe.calls
        assert memo.running_pid(path, probe=probe, now=0) is None
        assert probe.calls == before + 1
        assert memo.running_pid(path, probe=probe, now=1) is None
        assert probe.calls == before + 1, "bounding retention must preserve immediate reuse"
        assert 0 < len(memo._memo) <= 256, "retain at most 256 distinct path/probe keys"


def test_profiles_check_gateway_running_uses_the_memo(tmp_path, monkeypatch, alive):
    from hermes_cli import profiles
    calls = []

    def fake_get_running_pid(pid_path=None, *, cleanup_stale=True):
        calls.append(cleanup_stale)

    monkeypatch.setattr(status, "get_running_pid", fake_get_running_pid)
    monkeypatch.setattr(status, "multiplexer_liveness_for_profile", lambda profile_dir: None)
    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    for _ in range(5):
        assert profiles._check_gateway_running(home) is False
    assert calls == [False], "the PID-file rung must probe once while nothing changes, never cleaning"


# -- replay benchmark: budget 0.05% of one core (design §3: get_running_pid 0.4 -> 0.05) --------------------------

def _unguarded_io(monkeypatch) -> None:
    """Measure production cost: the suite's HomeIOGuard wraps os.stat/open in Python checks (3-4x a memo hit,
    which made this bench flake at ~1 ms per list under load). The wrappers keep the originals in __wrapped__."""
    import builtins
    import io
    for module, name in ((os, "stat"), (os, "lstat"), (builtins, "open"), (io, "open")):
        fn = getattr(module, name)
        monkeypatch.setattr(module, name, getattr(fn, "__wrapped__", fn))


def test_replay_budget_polled_profile_list(files, monkeypatch):
    """The measured stream: 11 profiles listed every 2 s by an open Desktop, no gateway changing state. A memo
    hit costs two stats plus one /proc read per profile; budget is per wall second of polling."""
    import fcntl
    _unguarded_io(monkeypatch)
    paths, held = [], []
    for n in range(11):
        d = files.parent / f"p{n}"
        d.mkdir()
        p = d / "gateway.pid"
        p.write_text("{}")
        lock = status._get_gateway_lock_path(p)
        lock.write_text("")
        h = open(lock, "a+")  # a real held lock: the portable path checks it
        fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(h)
        paths.append(p)
    pid = os.getpid()  # a real live process: exercise the real liveness path
    probe = _Probe(pid)
    try:
        for p in paths:
            memo.running_pid(p, probe=probe)
        best = float("inf")
        for _ in range(5):
            t0 = time.thread_time()
            for _ in range(50):
                for p in paths:
                    assert memo.running_pid(p, probe=probe) == pid
            best = min(best, (time.thread_time() - t0) / 50)
    finally:
        for h in held:
            h.close()
    assert probe.calls == len(paths)
    per_second = best / 2.0  # one profile list every 2 s
    assert per_second <= 0.0005, f"{best * 1000:.3f} ms per list = {per_second:.4%} of a core"
