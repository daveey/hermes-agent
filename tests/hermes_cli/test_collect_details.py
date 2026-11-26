"""Fixed, inclusive collect attribution must not alter metric semantics. [daveey]"""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_cpu as sc
from hermes_cli import loop_accounting as la
from hermes_cli import machine_admission_metrics as admission
from hermes_cli import machine_metrics as mm


@pytest.fixture
def clock(monkeypatch):
    la.reset()
    monkeypatch.setattr(sc, "_local", threading.local())
    monkeypatch.setattr(la, "scopes_enabled", lambda: False)
    now = [0.0]
    monkeypatch.setattr(sc.time, "thread_time", lambda: now[0])
    yield now
    la.reset()


def report(fn):
    return fn()


def test_real_collect_wiring_order_payload_and_nonadditivity(clock, monkeypatch, tmp_path):
    from hermes_cli import machine_drain_metrics as drain
    from hermes_cli import machine_persistent_jobs as persistent
    from hermes_cli import machine_throughput_metrics as throughput
    from hermes_cli import worker_compute_capacity as capacity
    from hermes_cli import kanban_sync_claims as claims

    calls = []

    def observed(name, result):
        def call(*args, **kwargs):
            calls.append(name)
            clock[0] += 1.0
            return result
        return call

    monkeypatch.setattr(capacity, "worker_enabled", observed("worker", False))
    monkeypatch.setattr(admission, "policy", observed("policy", {}))
    monkeypatch.setattr(mm, "active_card_runs", observed("census", 2))
    monkeypatch.setattr(mm._REPORT, "census", None, raising=False)
    monkeypatch.setattr(persistent, "collect", observed("persistent", {"persistent": 3}))
    monkeypatch.setattr(drain, "collect", observed("drain", {"drain": 1}))
    monkeypatch.setattr(claims, "claiming_bit", observed("claiming", 0))
    monkeypatch.setattr(throughput, "cached_counters", observed("throughput", {"tokens": 4}))
    monkeypatch.setattr(mm.os, "getloadavg", observed("load", (1.25, 0, 0)))
    monkeypatch.setattr(mm.os, "cpu_count", observed("cpus", 8))
    monkeypatch.setattr(mm, "_mem_used_pct", observed("memory", 25))
    monkeypatch.setattr(mm.shutil, "disk_usage", observed("disk", SimpleNamespace(free=mm._GB, total=2*mm._GB)))
    records = []
    original = la.record

    def record(name, **kw):
        records.append((name, kw.get("outer", True)))
        return original(name, **kw)

    monkeypatch.setattr(la, "record", record)
    out = sc.step(report, lambda: mm.collect(tmp_path))
    assert calls == ["worker", "policy", "census", "persistent", "drain", "claiming", "throughput",
                     "load", "cpus", "cpus", "memory", "disk"]
    assert out == {"active_card_runs": 2, "persistent": 3, "drain": 1, "claiming": 0,
                   "tokens": 4, "cpu_load1": 1.25, "cpus": 8, "mem_used_pct": 25,
                   "disk_free_gb": 1.0, "disk_total_gb": 2.0}
    stats = la.counters()
    expected = {"collect.worker": 1, "collect.admission": 2, "collect.policy": 1,
                "collect.census": 1, "collect.persistent": 1, "collect.drain": 1,
                "collect.claiming": 1, "collect.throughput": 1, "collect.load": 1,
                "collect.cpus": 2, "collect.memory": 1, "collect.disk": 1}
    for part, cpu in expected.items():
        key = "sync.detail/report." + part
        assert stats[key]["cpu_s"] == pytest.approx(cpu), part
        assert (key, False) in records
    assert sum(la._copy_accounted().values()) == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_admission_policy_and_census_fresh_each_call(clock, monkeypatch, enabled):
    settings = {"admission_version": 1} if enabled else {}
    monkeypatch.setattr(admission, "policy", lambda: settings)
    seen = []

    def count(**kwargs):
        seen.append(kwargs)
        clock[0] += 2
        if kwargs:
            kwargs["detail"]["pools"]["regular"] += len(seen)
        return len(seen)

    first = sc.step(report, lambda: admission.collect(count))
    second = sc.step(report, lambda: admission.collect(count))
    assert first["active_card_runs"] == 1 and second["active_card_runs"] == 2
    assert bool(seen[0]) is enabled
    if enabled:
        assert second == {"active_card_runs": 2, "admission_version": 1,
                          "active_regular_runs": 2, "active_priority_runs": 0}
    assert la.counters()["sync.detail/report.collect.census"]["cpu_s"] == 4
    assert la.counters()["sync.detail/report.collect.policy"]["runs"] == 2
    assert sum(la._copy_accounted().values()) == 0


def test_exception_records_subscope_without_replacing_error(clock, monkeypatch):
    error = ValueError("census")
    monkeypatch.setattr(admission, "policy", dict)

    def count():
        clock[0] += 3
        raise error

    with pytest.raises(ValueError) as raised:
        sc.step(report, lambda: admission.collect(count))
    assert raised.value is error
    assert la.counters()["sync.detail/report.collect.census"]["cpu_s"] == 3
    assert sum(la._copy_accounted().values()) == 0


@pytest.mark.parametrize("fallback", [False, True])
def test_kept_census_fixed_board_scopes(clock, monkeypatch, tmp_path, fallback):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as boards
    from hermes_cli import machine_report_census as census
    from hermes_cli import kanban_admission_pools as pools

    default = tmp_path / "kanban.db"
    other = tmp_path / "secret-board-name.db"
    default.touch()
    other.touch()
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(boards, "named_board_db_files", lambda: [("private", other)])
    monkeypatch.setattr(pools, "count_pools", lambda *a, **k: {"regular": 2, "priority": 0})
    reads = []

    def read(path, tag, fn, *, unchanged_ok):
        assert tag == census.TAG and fn is mm._running_rows and unchanged_ok
        reads.append(path)
        clock[0] += 1
        return None if fallback else (False, (False, None, [(0, "receipt")]))

    def uncached(*, detail):
        clock[0] += 5
        return 7

    detail = {"pools": {"regular": 0, "priority": 0}}
    count = census.census(SimpleNamespace(read=read), fallback=uncached)
    assert sc.step(report, lambda: count(detail=detail)) == (7 if fallback else 2)
    assert reads == ([default] if fallback else [default, other])
    stats = la.counters()
    assert stats["sync.detail/report.collect.census.read"]["cpu_s"] == (1 if fallback else 2)
    assert stats["sync.detail/report.collect.census.boards"]["runs"] == 1
    assert stats["sync.detail/report.collect.census.stat"]["runs"] == len(reads)
    if fallback:
        assert stats["sync.detail/report.collect.census.fallback"]["cpu_s"] == 5
    else:
        assert stats["sync.detail/report.collect.census.pools"]["runs"] == 1
        assert detail == {"pools": {"regular": 2, "priority": 0}, "excluded": 0}
    assert all("secret" not in key and "private" not in key for key in stats)
    assert sum(la._copy_accounted().values()) == 0
