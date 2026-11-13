"""Loop/route pass accounting (design t_01a1d563324d7e8e §2 P1). [daveey]"""

from __future__ import annotations

import json
import os
import threading
import time

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
    """macOS has no /proc and never reuses native thread ids: exited threads must still fold, or the map grows."""
    real_listdir = os.listdir

    def no_procfs(path="."):
        if str(path).startswith("/proc"):
            raise FileNotFoundError(path)
        return real_listdir(path)

    monkeypatch.setattr(la.os, "listdir", no_procfs)

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
