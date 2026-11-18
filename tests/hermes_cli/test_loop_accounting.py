"""Loop/route pass accounting (design t_01a1d563324d7e8e §2 P1). [daveey]"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import loop_accounting as la


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    la.stop()
    la.reset()
    yield
    la.stop()
    la.reset()


def _burn(seconds: float) -> None:
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def test_pass_counts_runs_skips_changes_fallbacks_rows():
    with la.pass_("loop") as p:
        p.change()
        p.rows(3)
    with la.pass_("loop") as p:
        p.skip()
    with la.pass_("loop", fallback=True, rows=2):
        pass
    c = la.counters()["loop"]
    assert (c["runs"], c["skips"], c["changes"], c["fallbacks"], c["rows"], c["errors"]) == (3, 1, 1, 1, 5, 0)


def test_pass_counts_an_error_and_reraises():
    with pytest.raises(ValueError):
        with la.pass_("boom"):
            raise ValueError("x")
    assert la.counters()["boom"]["errors"] == 1
    assert la.counters()["boom"]["runs"] == 1


OperationalError = sqlite3.OperationalError  # the class the chat-closeout live check saw


def test_errors_are_counted_by_exception_class():
    for exc in (OperationalError, OperationalError, KeyError):
        with pytest.raises(exc):
            with la.pass_("kinds"):
                raise exc("x")
    with la.pass_("kinds") as p:  # a handled fallback is no error
        p.fallback()
    c = la.counters()["kinds"]
    assert (c["runs"], c["errors"], c["fallbacks"]) == (4, 3, 1)
    assert c["error_kinds"] == {"OperationalError": 2, "KeyError": 1}
    with la.pass_("clean"):
        pass
    assert la.counters()["clean"]["error_kinds"] == {}


def test_error_kinds_cap_names_and_fold_the_rest_into_other():
    names = [f"E{i}" for i in range(la.MAX_ERROR_KINDS + 3)]
    for name in names + names[:2]:
        with pytest.raises(Exception):
            with la.pass_("many"):
                raise type(name, (Exception,), {})("x")
    c = la.counters()["many"]
    kinds = c["error_kinds"]
    assert len(kinds) == la.MAX_ERROR_KINDS + 1  # 8 class names plus "other"
    assert {n: kinds[n] for n in names[:la.MAX_ERROR_KINDS]} == dict.fromkeys(names[:la.MAX_ERROR_KINDS], 1) | {
        names[0]: 2, names[1]: 2}  # an already-kept class keeps counting under its own name
    assert kinds[la.ERROR_KIND_OTHER] == 3
    assert sum(kinds.values()) == c["errors"] == len(names) + 2


def test_record_and_span_errors_carry_a_kind_or_count_as_unknown():
    la.record("rec", error=True, error_kind="TimeoutError")
    la.record("rec", error=True)
    la.record("rec", error=False, error_kind="Ignored")
    la.span_close(la.span_open("span"), error=True)
    assert la.counters()["rec"]["error_kinds"] == {"TimeoutError": 1, la.ERROR_KIND_UNKNOWN: 1}
    assert la.counters()["span"]["error_kinds"] == {la.ERROR_KIND_UNKNOWN: 1}


@pytest.mark.parametrize("error", [True, False], ids=["error", "no-error"])
def test_span_close_counts_explicit_error_kind_only_for_errors(error):
    assert la.span_close(la.span_open("explicit-kind"), error=error, error_kind="ValueError") is True
    c = la.counters()["explicit-kind"]
    assert c["runs"] == 1
    assert c["errors"] == int(error)
    assert c["error_kinds"] == ({"ValueError": 1} if error else {})


def test_snapshot_carries_error_kinds_next_to_errors():
    with pytest.raises(OperationalError):
        with la.pass_("snap"):
            raise OperationalError("locked")
    loop = json.loads(json.dumps(la.snapshot()))["loops"]["snap"]
    assert (loop["errors"], loop["error_kinds"]) == (1, {"OperationalError": 1})


def test_cpu_is_thread_time_sum_and_max():
    with la.pass_("cpu"):
        _burn(0.02)
    with la.pass_("cpu"):
        time.sleep(0.05)  # health: allow HX013 -- wall time that is not CPU is the behaviour under test
    c = la.counters()["cpu"]
    assert c["cpu_s"] >= 0.02
    assert c["cpu_max_s"] >= 0.02
    assert c["cpu_s"] < 0.06
    assert c["wall_s"] >= 0.07


def test_timed_and_accounted_count_rows_and_keep_signature():
    import inspect

    assert la.timed("t", lambda n: list(range(n)), 4) == [0, 1, 2, 3]
    assert la.counters()["t"]["rows"] == 4

    @la.accounted("dec", rows=True)
    def work(a: int, b: str = "x") -> int:
        """doc"""
        return a

    assert work(7) == 7
    assert la.counters()["dec"]["rows"] == 7
    assert list(inspect.signature(work).parameters) == ["a", "b"]
    assert work.__doc__ == "doc"

    @la.accounted("norows")
    def other() -> list:
        return [1, 2]

    other()
    assert la.counters()["norows"]["rows"] == 0


def test_only_outermost_pass_adds_to_thread_accounted():
    with la.pass_("outer"):
        with la.pass_("inner"):
            _burn(0.02)
    c = la.counters()
    assert c["inner"]["cpu_s"] >= 0.02 and c["outer"]["cpu_s"] >= c["inner"]["cpu_s"]
    me = threading.get_native_id()
    acc = {t["tid"]: t["accounted_cpu_s"] for t in la.thread_cpu()}
    assert acc[me] == pytest.approx(c["outer"]["cpu_s"], abs=1e-5)


def test_span_crosses_calls_and_does_not_double_count_inner_passes():
    token = la.span_open("lease.x")
    with la.pass_("inside"):
        _burn(0.01)
    _burn(0.01)
    assert la.span_close(token) is True
    c = la.counters()
    assert c["lease.x"]["runs"] == 1 and c["lease.x"]["cpu_s"] >= 0.02
    me = threading.get_native_id()
    acc = {t["tid"]: t["accounted_cpu_s"] for t in la.thread_cpu()}
    assert acc[me] == pytest.approx(c["lease.x"]["cpu_s"], abs=1e-5)


def test_span_closed_on_another_thread_records_nothing():
    token = la.span_open("lease.y")
    out = []
    t = threading.Thread(target=lambda: out.append(la.span_close(token)))
    t.start()
    t.join()
    assert out == [False]
    assert "lease.y" not in la.counters()


def test_host_loop_lease_accounts_held_to_beat(monkeypatch, tmp_path):
    from hermes_cli import host_loop_lease as hll

    monkeypatch.setenv("HERMES_HOST_LOOP_LEASE", "1")
    lease = hll.HostLoopLease("acct-test", "gateway", interval=1.0)
    try:
        if not lease.held():
            pytest.skip("lease not acquirable in this sandbox")
        _burn(0.01)
        lease.beat()
        lease.beat()  # a beat with no new held() records nothing
    finally:
        lease.release()
    c = la.counters()["lease.acct-test"]
    assert c["runs"] == 1 and c["cpu_s"] >= 0.01


def test_host_loop_lease_pass_that_raised_before_beat_is_closed_as_an_error(monkeypatch):
    """A host pass that raises skips beat() (kanban_annotation_host.loop): the next true held() must close the
    stale span as one errored run, not let the next beat() record one run spanning both passes."""
    from hermes_cli import host_loop_lease as hll

    monkeypatch.setenv("HERMES_HOST_LOOP_LEASE", "1")
    lease = hll.HostLoopLease("acct-raise", "gateway", interval=1.0)
    try:
        if not lease.held():
            pytest.skip("lease not acquirable in this sandbox")
        _burn(0.01)  # this pass "raises": no beat()
        assert lease.held()
        lease.beat()
    finally:
        lease.release()
    c = la.counters()["lease.acct-raise"]
    assert (c["runs"], c["errors"]) == (2, 1)


def test_fork_with_a_nested_pass_open_keeps_counting_outermost_passes():
    """Inherited nested passes unwind to zero, so later outermost passes still count."""
    with la.pass_("outer"):
        with la.pass_("inner"):
            la._after_fork_child()  # what os.fork runs in the child, here on this thread
    assert la._local.depth == 0
    with la.pass_("after"):
        _burn(0.01)
    me = threading.get_native_id()
    acc = {t["tid"]: t["accounted_cpu_s"] for t in la.thread_cpu()}
    assert acc.get(me, 0.0) >= 0.01


def test_reused_thread_id_does_not_inherit_the_exited_threads_cpu(monkeypatch):
    """A Linux tid reused before the next snapshot must start at 0, or unaccounted_live_cpu_s under-reports."""
    monkeypatch.setattr(la, "_native_id", lambda: 424242)  # both threads get the same native id
    seen: list = []

    def first():
        with la.pass_("first"):
            _burn(0.03)

    def second():
        with la.pass_("second"):
            pass
        with la._lock:
            seen.append(la._thread_accounted.get(424242, 0.0))

    for target in (first, second):
        t = threading.Thread(target=target)
        t.start()
        t.join()
    assert seen and seen[0] < 0.03, seen  # only the live thread's own CPU, not the exited one's
    assert la.snapshot()["process"]["accounted_cpu_s"] >= 0.03  # the exited thread's CPU is kept, as exited


def test_setting_survives_config_backend_unavailable(monkeypatch):
    """ConfigBackendUnavailable is a SystemExit: it must not escape start() / begin_process at boot."""
    from hermes_cli import config as config_mod
    from hermes_cli.config_backend import ConfigBackendUnavailable

    def unavailable():
        raise ConfigBackendUnavailable("backend missing")

    monkeypatch.setattr(config_mod, "load_config", unavailable)
    assert la._setting("interval_s", 7.0) == 7.0
    assert la.start("gateway") is True


def test_start_reads_the_interval_outside_the_lock(monkeypatch):
    """Every pass exit takes _lock: the config read for the interval must not run while start() holds it."""
    held_during_read: list = []

    def setting(key, default):
        if key == "interval_s":
            held_during_read.append(la._lock.locked())
            return 0.05
        return default

    monkeypatch.setattr(la, "_setting", setting)
    assert la.start("serve") is True
    assert held_during_read == [False]


def test_name_overflow_is_bounded(monkeypatch):
    monkeypatch.setattr(la, "MAX_NAMES", 3)
    for i in range(10):
        with la.pass_(f"n{i}"):
            pass
    c = la.counters()
    assert len(c) <= 4
    assert c[la.OVERFLOW]["runs"] == 7  # n0..n2 keep their names; n3..n9 fold


def test_per_thread_cpu_covers_each_thread():
    started = threading.Event()
    stop = threading.Event()

    def worker():
        with la.pass_("worker"):
            _burn(0.03)
        started.set()
        stop.wait(5)

    t = threading.Thread(target=worker, name="acct-worker")
    t.start()
    started.wait(5)
    try:
        rows = {r["name"]: r for r in la.thread_cpu()}
        assert "acct-worker" in rows
        if rows["acct-worker"]["cpu_s"] is not None:  # Linux /proc
            assert rows["acct-worker"]["cpu_s"] >= 0.02
        assert rows["acct-worker"]["accounted_cpu_s"] >= 0.03
    finally:
        stop.set()
        t.join()


def test_without_procfs_snapshot_folds_exited_threads(monkeypatch):
    """With no per-thread CPU source (no /proc, no Darwin proc_pidinfo) rows carry no CPU and native thread ids are
    never reused: exited threads must still fold, or the map grows. Both sources are hidden, so the test means the
    same on every host; macOS's real proc_pidinfo source is covered by the platforms("macos") test below. [daveey]"""
    real_listdir = os.listdir

    def no_procfs(path="."):
        if str(path).startswith("/proc"):
            raise FileNotFoundError(path)
        return real_listdir(path)

    monkeypatch.setattr(la.os, "listdir", no_procfs)
    # Mutation probe: intentionally leave Darwin's native CPU source live.

    def worker():
        with la.pass_("short-lived"):
            _burn(0.002)

    accounted_before_fold = 0.0
    for _ in range(5):
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with la._lock:
            accounted_before_fold = sum(la._thread_accounted.values()) + la._state.get("exited_accounted", 0.0)
        snap = la.snapshot()
        assert all(row["cpu_s"] is None for row in snap["threads"])
        assert len(la._thread_accounted) <= threading.active_count()
        assert len(snap["threads"]) <= threading.active_count()
        assert snap["process"]["accounted_cpu_s"] == pytest.approx(accounted_before_fold, abs=1e-5)
        assert snap["process"]["exited_threads_cpu_s"] is None
        assert snap["process"]["unaccounted_live_cpu_s"] is None
    assert la._state["exited_accounted"] >= 100 * 0.002 * 0.5


@pytest.mark.platforms("macos")
def test_darwin_thread_source_rows_carry_cpu_and_exited_threads_fold():
    """Real macOS: no /proc, so per-thread CPU comes from proc_pidinfo (80c0c4c277e6). Live rows carry measured CPU,
    the exited/unaccounted split is computed, and exited threads still fold out of the map. [daveey]"""
    assert not os.path.isdir("/proc/self/task")

    def worker():
        with la.pass_("short-lived"):
            _burn(0.002)

    for _ in range(5):
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with la._lock:
            accounted_before_fold = sum(la._thread_accounted.values()) + la._state.get("exited_accounted", 0.0)
        snap = la.snapshot()
        assert snap["threads"] and all(row["cpu_s"] is not None for row in snap["threads"])
        assert len(la._thread_accounted) <= threading.active_count()
        assert snap["process"]["accounted_cpu_s"] == pytest.approx(accounted_before_fold, abs=1e-5)
        assert snap["process"]["exited_threads_cpu_s"] is not None
        assert snap["process"]["unaccounted_live_cpu_s"] is not None
    assert la._state["exited_accounted"] >= 100 * 0.002 * 0.5


def test_snapshot_file_is_atomic_json_with_role_pid_and_counters(tmp_path):
    with la.pass_("loop") as p:
        p.rows(2)
    la._state["role"] = "gateway"
    path = la.write_snapshot(tmp_path / "s" / "123.json")
    data = json.loads(path.read_text())
    assert data["version"] == la.SCHEMA_VERSION
    assert data["role"] == "gateway"
    assert data["pid"] == os.getpid()
    assert data["loops"]["loop"]["rows"] == 2
    assert {"cpu_s", "live_threads_cpu_s", "accounted_cpu_s"} <= set(data["process"])
    assert data["threads"] and all("tid" in t for t in data["threads"])
    assert [p.name for p in path.parent.iterdir()] == ["123.json"]  # no temp file left behind


def test_default_snapshot_path_is_under_root_run(tmp_path):
    assert la.snapshot_path(42) == tmp_path / "home" / "run" / "loop-accounting" / "42.json"


def test_start_writes_periodically_and_stop_flushes(monkeypatch, tmp_path):
    monkeypatch.setattr(la, "_setting", lambda key, default: 0.05 if key == "interval_s" else default)
    assert la.start("serve") is True
    with la.pass_("route/x"):
        pass
    path = la.snapshot_path()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.02)
    assert path.exists()
    la.stop(write=True)
    data = json.loads(path.read_text())
    assert data["role"] == "serve"
    assert data["loops"]["route/x"]["runs"] == 1


def test_disabled_writer_keeps_counters(monkeypatch):
    monkeypatch.setattr(la, "_setting", lambda key, default: False if key == "enabled" else default)
    assert la.start("gateway") is False
    with la.pass_("still"):
        pass
    assert la.counters()["still"]["runs"] == 1
    assert not la.snapshot_path().exists()


def test_prune_removes_old_files_of_dead_pids(tmp_path):
    d = tmp_path / "snaps"
    d.mkdir()
    dead, mine = d / "999999999.json", d / f"{os.getpid()}.json"
    fresh = d / "999999998.json"
    for f in (dead, mine, fresh):
        f.write_text("{}")
    old = time.time() - la.PRUNE_AFTER_S - 10
    os.utime(dead, (old, old))
    os.utime(mine, (old, old))
    assert la.prune(d) == 1
    assert not dead.exists() and mine.exists() and fresh.exists()


def test_begin_process_starts_the_writer_for_long_lived_kinds_only(monkeypatch):
    from hermes_cli.observability import shared_metrics_process as smp

    started = []
    monkeypatch.setattr(la, "start", started.append)
    for kind in ("cli", "cron", "gateway"):
        monkeypatch.setattr(smp, "_STATE", {})
        monkeypatch.setattr("hermes_cli.observability.shared_metrics_update._collection_on", lambda: False)
        smp.begin_process(kind)
    assert started == ["gateway"]


def test_sync_timing_board_hooks_account_each_board():
    from types import SimpleNamespace

    from hermes_cli import kanban_sync_timing as st

    results: list = []
    tok = st.board_start("main", results)
    results.append(SimpleNamespace(pulled=4, skipped=False, error=None))
    st.board_end(tok, results)
    tok = st.board_start("main", results)
    results.append(SimpleNamespace(pulled=0, skipped=True, error="busy"))
    st.board_end(tok, results)
    tok = st.board_start("main", results)
    results.append(SimpleNamespace(pulled=0, skipped=False, error="boom"))
    st.board_end(tok, results)
    c = la.counters()["sync.board/main"]
    assert (c["runs"], c["rows"], c["skips"], c["errors"]) == (3, 4, 1, 1)


# -- mutant gaps from the #2401 post-merge check (t_01a1d697acbb03f7) ------------------------------------------

_PROCFS = os.path.isdir("/proc/self/task")


def test_write_snapshot_replaces_a_same_directory_temp_file_onto_the_target(monkeypatch, tmp_path):
    """A reader (the hub collector) must never see a half-written file: write a temp file beside the target and
    os.replace it, and a snapshot that fails mid-write leaves the previous file intact."""
    target = tmp_path / "s" / "7.json"
    la.write_snapshot(target)
    real_replace = os.replace
    calls: list = []

    def recording_replace(src, dst):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(la.os, "replace", recording_replace)
    with la.pass_("after-first-write"):
        pass
    la.write_snapshot(target)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == target and src != target and src.parent == target.parent
    assert "after-first-write" in json.loads(target.read_text())["loops"]

    old = target.read_text()

    def broken_snapshot():
        raise RuntimeError("snapshot failed mid-write")

    monkeypatch.setattr(la, "snapshot", broken_snapshot)
    with pytest.raises(RuntimeError):
        la.write_snapshot(target)
    assert target.read_text() == old
    assert [p.name for p in target.parent.iterdir()] == ["7.json"]  # the temp file is cleaned up


def test_prune_keeps_an_old_file_while_its_pid_lives_and_removes_it_once_reaped(tmp_path):
    d = tmp_path / "snaps"
    d.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    assert child.stdin is not None
    snap = d / f"{child.pid}.json"
    try:
        snap.write_text("{}")
        old = time.time() - la.PRUNE_AFTER_S - 10
        os.utime(snap, (old, old))
        assert la.prune(d) == 0
        assert snap.exists()
    finally:
        child.stdin.close()
        child.wait(timeout=10)
    assert la.prune(d) == 1
    assert not snap.exists()


def test_sync_timing_board_end_without_a_result_counts_an_error():
    """A board whose service appended no result (it raised before reporting) is a failed pass."""
    from hermes_cli import kanban_sync_timing as st

    results: list = []
    st.board_end(st.board_start("x", results), results)
    c = la.counters()["sync.board/x"]
    assert (c["runs"], c["errors"], c["skips"], c["rows"]) == (1, 1, 0, 0)


def test_writer_prunes_dead_snapshots(monkeypatch):
    pruned = threading.Event()
    monkeypatch.setattr(la, "_setting", lambda key, default: 0.05 if key == "interval_s" else default)
    monkeypatch.setattr(la, "prune", lambda *a, **k: pruned.set() or 0)
    assert la.start("gateway") is True
    assert pruned.wait(5), "the snapshot writer never pruned"


def test_record_counts_outcomes_and_accounts_cpu_only_for_an_outermost_record():
    me = threading.get_native_id()

    def accounted() -> float:
        with la._lock:
            return la._thread_accounted.get(me, 0.0)

    la.record("r", cpu_s=0.5, rows=3, skipped=True, error=True)
    c = la.counters()["r"]
    assert (c["runs"], c["skips"], c["rows"], c["errors"]) == (1, 1, 3, 1)
    assert c["cpu_s"] == pytest.approx(0.5)
    assert accounted() == pytest.approx(0.5)

    la.record("r", cpu_s=0.5, outer=False)
    assert accounted() == pytest.approx(0.5)

    with la.pass_("open"):
        la.record("r", cpu_s=0.5)  # inside an open pass: the pass accounts its own CPU, not this one's
    assert la.counters()["r"]["runs"] == 3
    assert accounted() == pytest.approx(0.5 + la.counters()["open"]["cpu_s"], abs=1e-6)


def test_stop_with_write_flushes_a_snapshot_the_writer_has_not_written_yet(monkeypatch):
    monkeypatch.setattr(la, "_setting", lambda key, default: 60.0 if key == "interval_s" else default)
    assert la.start("serve") is True
    with la.pass_("route/y"):
        pass
    path = la.snapshot_path()
    assert not path.exists()  # a 60 s writer has not written yet
    la.stop(write=True)
    data = json.loads(path.read_text())
    assert data["loops"]["route/y"]["runs"] == 1


@pytest.mark.skipif(not _PROCFS, reason="thread CPU needs /proc/self/task")
def test_snapshot_reports_live_cpu_spent_outside_any_pass_as_unaccounted():
    burned = threading.Event()
    release = threading.Event()

    def worker():
        _burn(0.2)
        burned.set()
        release.wait(10)

    t = threading.Thread(target=worker, name="acct-unaccounted")
    t.start()
    try:
        assert burned.wait(10)
        snap = la.snapshot()
    finally:
        release.set()
        t.join()
    row = next(r for r in snap["threads"] if r["name"] == "acct-unaccounted")
    assert row["cpu_s"] >= 0.15 and row["accounted_cpu_s"] == 0.0
    assert snap["process"]["unaccounted_live_cpu_s"] >= 0.15


@pytest.mark.skipif(not _PROCFS, reason="thread CPU needs /proc/self/task")
def test_snapshot_reports_cpu_of_exited_threads():
    t = threading.Thread(target=_burn, args=(0.1,))
    t.start()
    t.join()
    assert la.snapshot()["process"]["exited_threads_cpu_s"] >= 0.05


# -- exited-tid fold paths and fork clocks, from the #2630 post-merge check (t_01a1d6fffa79f116) ---------------


def _stale_exit_queued_for(tid: int, cpu: float) -> None:
    """A thread that accounted ``cpu`` under ``tid`` has exited and queued its fold; this live thread now has
    the same native id (Linux reuses tids)."""
    with la._lock:
        la._thread_accounted[tid] = cpu
    la._exited_tids.append(tid)


def test_span_open_folds_a_queued_exit_before_reading_the_baseline(monkeypatch):
    """span_open must fold first: a stale baseline makes span_close book the exited thread's CPU again."""
    clock = [100.0]
    monkeypatch.setattr(la, "_thread_time", lambda: clock[0])
    me = la._own_tid()
    _stale_exit_queued_for(me, 5.0)
    token = la.span_open("lease.reused")
    with la.pass_("inside"):  # an outermost pass inside the span: its exit folds the queue
        clock[0] += 1.0
    clock[0] += 2.0
    assert la.span_close(token) is True
    with la._lock:
        assert la._thread_accounted[me] == pytest.approx(3.0)  # the span's CPU, once
        assert la._state["exited_accounted"] == pytest.approx(5.0)


def test_thread_cpu_folds_a_queued_exit_before_reporting_accounted_cpu():
    me = la._own_tid()
    _stale_exit_queued_for(me, 5.0)
    rows = {r["tid"]: r for r in la.thread_cpu()}
    assert rows.get(me, {}).get("accounted_cpu_s", 0.0) == 0.0  # not the exited thread's 5 s
    assert la._state["exited_accounted"] == pytest.approx(5.0)


def test_snapshot_folds_an_exit_queued_after_it_listed_the_threads(monkeypatch):
    """A thread can exit between snapshot's thread listing and its lock: its tid is live in the listing (reused),
    so only snapshot's own fold moves its CPU to exited instead of counting it as this live thread's."""
    me = la._own_tid()
    with la._lock:
        la._thread_accounted[me] = 5.0
    real_thread_cpu = la.thread_cpu

    def listing_then_exit():
        rows = real_thread_cpu()
        la._exited_tids.append(me)
        return rows

    monkeypatch.setattr(la, "thread_cpu", listing_then_exit)
    snap = la.snapshot()
    with la._lock:
        assert me not in la._thread_accounted
    assert la._state["exited_accounted"] == pytest.approx(5.0)
    assert snap["process"]["accounted_cpu_s"] == pytest.approx(5.0)


def test_after_fork_child_starts_with_an_empty_exit_queue():
    """The child must not inherit the parent's queued exits (nor the one its own hook queues by dropping the
    forking thread's exit mark): they are the parent's tids, not the child's."""
    la._exited_tids.extend([111, 222])
    la._own_tid()
    la._after_fork_child()
    assert not la._exited_tids


def test_reset_clears_the_exit_queue():
    la._exited_tids.extend([111, 222])
    la.reset()
    assert not la._exited_tids


def test_an_exit_mark_for_another_tid_is_replaced_so_the_thread_folds_its_own_tid():
    """A mark left under a tid this thread no longer has (a fork child without the at-fork hook) would queue the
    wrong tid at exit, so a later thread given this tid inherits the exited thread's CPU."""
    seen: list = []

    def worker():
        la._local.exit_mark = la._ThreadExit(999_999_001)
        seen.append(threading.get_native_id())
        la.record("w", cpu_s=2.0)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    la.thread_cpu()  # folds the exits queued so far
    with la._lock:
        assert seen[0] not in la._thread_accounted
    assert la._state["exited_accounted"] == pytest.approx(2.0)


def test_fork_child_does_not_book_negative_cpu_for_pre_fork_passes(monkeypatch):
    """The child's thread clock restarts at the fork: passes open across it book only the child's CPU and wall
    since the fork, never parent mark minus child clock (live probe: outer cpu_s -0.115 s)."""
    clock = [50.0]
    monkeypatch.setattr(la, "_thread_time", lambda: clock[0])
    me = threading.get_native_id()
    with la.pass_("outer"):
        with la.pass_("inner"):
            clock[0] = 0.0  # the child's thread clock
            la._after_fork_child()  # what os.fork runs in the child, here on this thread
            clock[0] += 0.25
        clock[0] += 0.5
    c = la.counters()
    assert c["inner"]["cpu_s"] == pytest.approx(0.25)
    assert c["outer"]["cpu_s"] == pytest.approx(0.75)
    assert c["outer"]["wall_s"] >= 0.0 and c["inner"]["wall_s"] >= 0.0
    with la._lock:
        assert la._thread_accounted[me] == pytest.approx(0.75)
    with la.pass_("after"):  # a pass opened in the child measures from its own mark
        clock[0] += 1.0
    assert la.counters()["after"]["cpu_s"] == pytest.approx(1.0)


@pytest.mark.parametrize("forks", [1, 2])
def test_child_opened_nested_pass_is_not_accounted_twice(monkeypatch, forks):
    clock = [50.0]
    monkeypatch.setattr(la, "_thread_time", lambda: clock[0])
    with la.pass_("outer"):
        with la.pass_("inherited_inner"):
            for _ in range(forks):
                clock[0] = 0.0
                la._after_fork_child()
            with la.pass_("child_inner"):
                clock[0] += 0.25
            la.record("child_record", cpu_s=0.125)
            clock[0] += 0.5
    assert la._local.depth == 0
    assert la.counters()["child_inner"]["cpu_s"] == pytest.approx(0.25)
    assert la.snapshot()["process"]["accounted_cpu_s"] == pytest.approx(0.75)
    with la.pass_("after"):
        clock[0] += 0.5
    assert la.snapshot()["process"]["accounted_cpu_s"] == pytest.approx(1.25)


@pytest.mark.parametrize("forks", [1, 2])
def test_inherited_passes_subtract_nonzero_child_fork_clocks(monkeypatch, forks):
    """Only elapsed time since the latest child hook belongs to inherited passes."""
    clock = [50.0, 1000.0]
    monkeypatch.setattr(la, "_thread_time", lambda: clock[0])
    monkeypatch.setattr(la, "_perf", lambda: clock[1])
    with la.pass_("outer"):
        clock[:] = [51.0, 1002.0]
        with la.pass_("inherited_inner"):
            for generation in range(forks):
                # Child CPU may have advanced before this hook; wall never resets.
                clock[:] = [2.0 + generation, 1100.0 + 100.0 * generation]
                la._after_fork_child()
            clock[0] += 0.25
            clock[1] += 2.0
            with la.pass_("child_inner"):
                clock[0] += 0.5
                clock[1] += 3.0
            la.record("child_record", cpu_s=0.125, wall_s=0.25)
        clock[0] += 1.0
        clock[1] += 4.0
    counters = la.counters()
    for name, cpu, wall in (("inherited_inner", 0.75, 5.0), ("outer", 1.75, 9.0),
                            ("child_inner", 0.5, 3.0), ("child_record", 0.125, 0.25)):
        assert counters[name]["runs"] == 1
        assert counters[name]["cpu_s"] == pytest.approx(cpu), name
        assert counters[name]["cpu_max_s"] == pytest.approx(cpu), name
        assert counters[name]["wall_s"] == pytest.approx(wall), name
    assert la.snapshot()["process"]["accounted_cpu_s"] == pytest.approx(1.75)
    with la.pass_("after"):
        clock[0] += 0.5
        clock[1] += 6.0
    assert la.counters()["after"]["cpu_s"] == pytest.approx(0.5)
    assert la.counters()["after"]["wall_s"] == pytest.approx(6.0)
    assert la.snapshot()["process"]["accounted_cpu_s"] == pytest.approx(2.25)


@pytest.mark.platforms("posix")
def test_real_fork_subtracts_nonzero_clocks_from_inherited_passes():
    # A subprocess confines the registered at-fork hook and child assertions.
    result = subprocess.run([sys.executable, "-c", '''
import os
import sqlite3
from hermes_cli import loop_accounting as la

clock = [50.0, 1000.0]
la._thread_time = lambda: clock[0]
la._perf = lambda: clock[1]
la.reset()
with la.pass_("outer"):
    with la.pass_("inner"):
        # Both clocks are nonzero when os.fork invokes the real registered hook.
        clock[:] = [2.0, 1100.0]
        pid = os.fork()
        if pid == 0:
            clock[:] = [2.25, 1102.0]
            with la.pass_("child_inner"):
                clock[:] = [2.75, 1105.0]
            la.record("child_record", cpu_s=0.125, wall_s=0.25)
    if pid == 0:
        clock[:] = [3.75, 1109.0]
if pid == 0:
    try:
        counters = la.counters()
        for name, cpu, wall in (("inner", 0.75, 5.0), ("outer", 1.75, 9.0),
                                ("child_inner", 0.5, 3.0), ("child_record", 0.125, 0.25)):
            actual = counters[name]
            assert (actual["runs"], actual["cpu_s"], actual["wall_s"]) == (1, cpu, wall), (name, actual)
        assert la.snapshot()["process"]["accounted_cpu_s"] == 1.75
        with la.pass_("after"):
            clock[:] = [4.25, 1115.0]
        assert la.counters()["after"]["cpu_s"] == 0.5
        assert la.counters()["after"]["wall_s"] == 6.0
        assert la.snapshot()["process"]["accounted_cpu_s"] == 2.25
    except BaseException:
        import traceback
        traceback.print_exc()
        os._exit(1)
    os._exit(0)
_, status = os.waitpid(pid, 0)
assert os.waitstatus_to_exitcode(status) == 0, status
'''], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def _fork_inside_nested_passes() -> int:
    with la.pass_("outer"):
        _burn(0.1)  # the parent's thread clock is well past the child's at the fork
        with la.pass_("inner"):
            pid = os.fork()
            if pid == 0:
                with la.pass_("child_inner"):
                    _burn(0.01)
    return pid


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_real_fork_child_books_no_negative_cpu_for_pre_fork_passes():
    read_fd, write_fd = os.pipe()
    pid = _fork_inside_nested_passes()
    if pid == 0:  # child: report, then leave without running any more of pytest
        code = 1
        try:
            os.close(read_fd)
            c = la.counters()
            snap = la.snapshot()
            os.write(write_fd, json.dumps({"outer": c["outer"]["cpu_s"], "inner": c["inner"]["cpu_s"],
                                           "accounted": snap["process"]["accounted_cpu_s"]}).encode())
            code = 0
        finally:
            os._exit(code)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as fh:
        raw = fh.read()
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    child = json.loads(raw)
    assert min(child.values()) >= 0.0, child
    assert child["accounted"] == pytest.approx(child["outer"]), child
    assert child["inner"] >= 0.005, child  # the child's own CPU inside the pass is booked
