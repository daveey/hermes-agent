"""Loop accounting: per-``threading.Thread``-target residual (t_01a1dee5bc2c9d6a)."""

from __future__ import annotations

import threading
import time

import pytest

from hermes_cli import loop_accounting as la

linux_procfs = pytest.mark.platforms("linux")  # per-thread CPU via procfs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    la.stop()
    la.reset()
    yield
    la.uninstrument()
    la.stop()
    la.reset()


def _burn(seconds: float) -> None:
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def _hold(name: str, target) -> threading.Event:
    """Start a thread running ``target`` that then stays alive until the returned event is set."""
    done, release = threading.Event(), threading.Event()

    def body() -> None:
        target()
        done.set()
        release.wait(30)

    # ``body`` is the Thread target, so wrap it: the key is body's qualname for every _hold thread.
    th = threading.Thread(target=body, name=name, daemon=True)
    th.start()
    assert done.wait(30)
    return release


def unscoped_daemon_loop():
    _burn(0.4)


def test_run_is_wrapped_once_and_restored():
    orig = threading.Thread.run
    assert la.instrument_thread_targets() is True
    assert la.instrument_thread_targets() is False
    assert threading.Thread.run is not orig
    la.uninstrument()
    assert threading.Thread.run is orig


@linux_procfs
def test_live_unscoped_target_cpu_is_named_and_scoped_is_not():
    la.instrument_thread_targets()
    release = threading.Event()
    done = threading.Event()

    def unscoped_worker():
        _burn(0.4)
        done.set()
        release.wait(30)

    done2 = threading.Event()

    def scoped_worker():
        with la.pass_("tt.scoped"):
            _burn(0.4)
        done2.set()
        release.wait(30)

    for fn in (unscoped_worker, scoped_worker):
        threading.Thread(target=fn, daemon=True).start()
    assert done.wait(30) and done2.wait(30)
    try:
        targets = la.snapshot()["residual"]["targets"]
    finally:
        release.set()
    un = next(v for k, v in targets.items() if k.endswith(".unscoped_worker"))
    sc = next(v for k, v in targets.items() if k.endswith(".scoped_worker"))
    assert abs(un["unaccounted_cpu_s"] - 0.4) <= 0.04
    assert un["live_threads"] == 1
    assert sc["unaccounted_cpu_s"] <= 0.04 and sc["accounted_cpu_s"] >= 0.35


@linux_procfs
def test_exited_thread_lands_in_its_target_exited_count():
    la.instrument_thread_targets()

    def short_lived():
        _burn(0.2)

    th = threading.Thread(target=short_lived)
    th.start()
    th.join()
    targets = la.snapshot()["residual"]["targets"]
    row = next(v for k, v in targets.items() if k.endswith("short_lived"))
    assert row["exited_threads"] == 1 and row["live_threads"] == 0
    assert abs(row["unaccounted_cpu_s"] - 0.2) <= 0.04
    assert row["unaccounted_cpu_s"] >= 0.0


@linux_procfs
def test_nested_scopes_are_not_counted_twice_in_a_target():
    la.instrument_thread_targets()

    def nested():
        with la.scope("tt.outer"):
            with la.pass_("tt.inner"):
                _burn(0.3)
            _burn(0.1)

    th = threading.Thread(target=nested)
    th.start()
    th.join()
    row = next(v for k, v in la.snapshot()["residual"]["targets"].items() if k.endswith("nested"))
    assert 0.38 <= row["accounted_cpu_s"] <= 0.45  # once, not 0.7
    assert row["unaccounted_cpu_s"] <= 0.05


def test_target_names_are_bounded_and_lambdas_are_told_apart():
    la.instrument_thread_targets()
    fns = []
    for i in range(la.MAX_TARGET_KEYS + 5):
        ns: dict = {}
        exec(f"def f{i}():\n    pass\n", {"__name__": "gen"}, ns)
        fns.append(ns[f"f{i}"])
    for fn in fns:
        th = threading.Thread(target=fn)
        th.start()
        th.join()
    with la._lock:
        names = set(la._target_exited)
    assert len(names) <= la.MAX_TARGET_KEYS + 1
    assert la.TARGET_OTHER in names
    a = la._target_key(lambda: None)
    b = la._target_key(lambda: None)
    assert a != b


def test_threads_without_target_and_failing_targets_still_run_and_book():
    la.instrument_thread_targets()

    class Sub(threading.Thread):
        def run(self):
            self.ran = True

    s = Sub()
    s.start()
    s.join()
    assert s.ran

    def boom():
        raise ValueError("x")

    errs = []
    old = threading.excepthook
    threading.excepthook = lambda a: errs.append(a.exc_type)
    try:
        th = threading.Thread(target=boom)
        th.start()
        th.join()
    finally:
        threading.excepthook = old
    assert errs == [ValueError]
    assert any(k.endswith("boom") for k in la._target_exited)
    assert not la._thread_targets


def test_flag_off_leaves_thread_run_alone(monkeypatch):
    orig = threading.Thread.run
    monkeypatch.setattr(la, "_setting", lambda key, default: False if key == "thread_targets" else default)
    la.instrument()
    assert threading.Thread.run is orig


@linux_procfs
def test_user_system_split_matches_cpu_and_family_sum_still_reconciles():
    la.instrument_thread_targets()
    release = _hold("tt-split_1", lambda: _burn(0.3))
    try:
        snap = la.snapshot()
    finally:
        release.set()
    for t in snap["threads"]:
        assert abs(t["user_s"] + t["system_s"] - t["cpu_s"]) <= 0.011
    res = snap["residual"]
    fam = res["families"]["tt-split"]
    assert fam["user_s"] + fam["system_s"] == pytest.approx(fam["cpu_s"], abs=0.02)
    total = sum(f["unaccounted_cpu_s"] for f in res["families"].values()) + res["exited"]["unaccounted_cpu_s"]
    assert total == pytest.approx(res["unaccounted_cpu_s"], abs=1e-3)


def test_reused_native_id_does_not_inherit_exited_accounting(monkeypatch):
    la.instrument_thread_targets()
    monkeypatch.setattr(la, "_native_id", lambda: 123456789)
    monkeypatch.setattr(la, "_thread_time", lambda: 0.2)
    # The previous lifetime has ended but no snapshot/new scope has folded it.
    la._thread_accounted[123456789] = 0.2
    la._exited_tids.append(123456789)

    def replacement():
        pass

    th = threading.Thread(target=replacement)
    th.start()
    th.join()
    row = la._target_exited[la._target_key(replacement)]
    assert row["accounted_cpu_s"] == 0.0
    assert row["unaccounted_cpu_s"] == pytest.approx(0.2)


def test_reused_native_id_nested_scopes_do_not_double_count(monkeypatch):
    la.instrument_thread_targets()
    monkeypatch.setattr(la, "_native_id", lambda: 123456789)
    la._thread_accounted[123456789] = 0.2
    la._exited_tids.append(123456789)
    ticks = iter((0.0, 0.0, 0.1, 0.2, 0.2))
    monkeypatch.setattr(la, "_thread_time", lambda: next(ticks))

    def replacement():
        with la.scope("replacement.outer"):
            with la.scope("replacement.inner"):
                pass

    th = threading.Thread(target=replacement)
    th.start()
    th.join()
    assert la.counters()["replacement.inner"]["cpu_s"] == pytest.approx(0.1)
    assert la.counters()["replacement.outer"]["cpu_s"] == pytest.approx(0.1)
    assert la._target_exited[la._target_key(replacement)]["accounted_cpu_s"] == pytest.approx(0.2)


@pytest.mark.parametrize("carrier", ["partial", "context_partial"])
@pytest.mark.parametrize("args_type", [tuple, list])
def test_thread_carriers_preserve_sequence_args_attribution(carrier, args_type):
    from contextvars import Context
    from functools import partial

    la.instrument_thread_targets()
    seen = []

    def original(first, second):
        seen.append((first, second))

    target = partial(original, 1)
    args = args_type([2])
    if carrier == "context_partial":
        target = partial(Context().run, target)
    th = threading.Thread(target=target, args=args)
    th.start()
    th.join()
    assert seen == [(1, 2)]
    assert la._target_key(original) in la._target_exited
    assert la._target_exited[la._target_key(original)]["threads"] == 1


def test_context_thread_names_original_target():
    from contextvars import Context

    la.instrument_thread_targets()

    def original(value):
        assert value == 42

    th = threading.Thread(target=Context().run, args=(original, 42))
    th.start()
    th.join()
    assert la._target_key(original) in la._target_exited
    assert not any("Context.run" in key for key in la._target_exited)


@pytest.mark.parametrize("clear", [la.reset, la._after_fork_child])
def test_lifetime_reset_clears_both_target_maps(clear):
    la._thread_targets[123] = "old.target"
    la._target_exited["old.target"] = {"threads": 1, "cpu_s": 0.2}
    clear()
    assert la._thread_targets == {}
    assert la._target_exited == {}


def test_thread_start_overhead_is_small():
    """One wrapped thread lifecycle adds two lock rounds: pin the added cost under 50 µs per thread."""
    def noop():
        pass

    def per_thread(n: int) -> float:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            for _ in range(n):
                th = threading.Thread(target=noop)
                th.start()
                th.join()
            best = min(best, (time.perf_counter() - t0) / n)
        return best

    base = per_thread(300)
    la.instrument_thread_targets()
    on = per_thread(300)
    assert on - base <= 50e-6, f"{(on - base) * 1e6:.1f} µs added per thread"
