"""Passive, bounded loop-probe diagnostics must never reuse an answer."""
from pathlib import Path
from unittest.mock import Mock
import threading

import pytest

from hermes_cli import kanban_sync_cpu as cpu, loop_accounting as la
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_sync_work import SyncWork


@pytest.fixture
def diag(monkeypatch):
    from hermes_cli import kanban_sync_probe_diag as module
    monkeypatch.setattr(module, "_names", set())
    module.enter()
    la.reset()
    yield module
    module.leave()
    la.reset()


def _as(step, fn, *args):
    def call():
        return fn(*args)
    call.__name__ = step
    return cpu.step(call)


def test_count_hit_miss_and_age_are_nonadditive(diag, monkeypatch):
    path = Path("/board")
    diag.install([("a", path)])
    clock = [100.0]
    monkeypatch.setattr(diag.time, "monotonic", lambda: clock[0])
    records = []
    monkeypatch.setattr(la, "record", lambda name, **kw: records.append((name, kw)))
    _as("wait", diag.note, path, (1, None))
    clock[0] = 100.125
    _as("select", diag.note, path, (1, (1,)))
    clock[0] = 101
    _as("select", diag.note, path, None)
    probes = [(n, kw) for n, kw in records if n.startswith("sync.probe_")]
    assert [n for n, _ in probes] == [
        "sync.probe_count/slug/a", "sync.probe_count/slug/a",
        "sync.probe_reuse_hit/slug/a", "sync.probe_wait_age_ms/slug/a",
        "sync.probe_count/slug/a", "sync.probe_reuse_miss/slug/a",
        "sync.probe_wait_age_ms/slug/a"]
    assert all(kw["outer"] is False and kw["cpu_s"] == 0 for _, kw in probes)
    assert [kw["rows"] for n, kw in probes if "age_ms" in n] == [125, 1000]


@pytest.mark.parametrize("answer", [None, (1, None), (1, ())])
def test_negative_wait_does_not_skip_pending(diag, monkeypatch, answer):
    path = Path("/board")
    diag.install([("a", path)])
    monkeypatch.setattr(diag.time, "monotonic", lambda: 100.0)
    work = SyncWork(1000)
    boards = [("a", path)]
    assert work.select(boards, 99, lambda paths: False) == boards
    _as("wait", diag.note, path, answer)
    pending = Mock(return_value=True)
    assert _as("select", work.select, boards, 100, pending) == boards
    pending.assert_called_once_with([path])


def test_short_circuit_counts_only_queried_board(diag, monkeypatch):
    a, b = Path("/a"), Path("/b")
    diag.install([("a", a), ("b", b)])
    query = Mock(return_value=(1, (1,)))
    monkeypatch.setattr(client._WATCH, "query", query)
    assert _as("wait", client.outbox_pending, [a, b])
    query.assert_called_once()
    counters = la.counters()
    assert counters["sync.probe_count/slug/a"]["runs"] == 1
    assert "sync.probe_count/slug/b" not in counters


def test_unmapped_and_slug_cap(diag):
    boards = [(f"b{i}", Path(f"/b{i}")) for i in range(34)]
    diag.install(boards + [("bad/sl ug", Path("/bad"))])
    for _, path in boards:
        diag.note(path, None)
    diag.note(Path("/secret/path"), None)
    diag.note(Path("/bad"), None)
    rows = {n: r for n, r in la.counters().items() if n.startswith("sync.probe_count/")}
    assert len(rows) == 33
    assert rows["sync.probe_count/slug/unmapped"]["runs"] == 4
    assert not any("secret" in n or "bad/" in n for n in rows)


def test_series_cap_survives_tenures_and_known_slugs_stay_named(diag):
    diag.install([(f"b{i}", Path(f"/b{i}")) for i in range(32)])
    diag.leave()
    diag.enter()
    diag.install([("new", Path("/new")), ("b0", Path("/b0"))])
    diag.note(Path("/new"), None)
    diag.note(Path("/b0"), None)
    rows = la.counters()
    assert rows["sync.probe_count/slug/unmapped"]["runs"] == 1
    assert rows["sync.probe_count/slug/b0"]["runs"] == 1
    assert "sync.probe_count/slug/new" not in rows


def test_invalid_slug_rejected_before_capacity(diag):
    diag.install([("x" * 65, Path("/long")), ("bad/name", Path("/bad"))])
    diag.note(Path("/long"), None)
    diag.note(Path("/bad"), None)
    assert set(la.counters()) == {"sync.probe_count/slug/unmapped"}
    assert la.counters()["sync.probe_count/slug/unmapped"]["runs"] == 2


def test_other_threads_and_after_leave_do_not_count(diag):
    path = Path("/board")
    diag.install([("a", path)])
    thread = threading.Thread(target=diag.note, args=(path, None))
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    diag.leave()
    diag.note(path, None)
    assert not any(n.startswith("sync.probe_") for n in la.counters())


@pytest.mark.parametrize("prior,age,expected", [(True, 0.1, "miss"), (False, 0.5, "hit"), (False, 0.501, "miss")])
def test_wait_freshness_and_positive_answer(diag, monkeypatch, prior, age, expected):
    path = Path("/a")
    diag.install([("a", path)])
    clock = [100.0]
    monkeypatch.setattr(diag.time, "monotonic", lambda: clock[0])
    _as("wait", diag.note, path, (1, (1,) if prior else None))
    clock[0] += age
    _as("select", diag.note, path, (1, None))
    assert la.counters()[f"sync.probe_reuse_{expected}/slug/a"]["runs"] == 1
    # A select query must not replace the positive/old wait answer.
    _as("select", diag.note, path, (1, None))
    assert la.counters()[f"sync.probe_reuse_{expected}/slug/a"]["runs"] == 2


def test_submillisecond_age_and_real_unmapped(diag, monkeypatch):
    path = Path("/a")
    diag.install([("unmapped", path)])
    clock = [100.0]
    monkeypatch.setattr(diag.time, "monotonic", lambda: clock[0])
    _as("wait", diag.note, path, None)
    clock[0] += 0.0001
    _as("select", diag.note, path, None)
    assert la.counters()["sync.probe_wait_age_ms/slug/unmapped"]["rows"] == 0
    assert la.counters()["sync.probe_wait_age_ms/slug/unmapped"]["runs"] == 1


def test_note_failure_never_changes_query_result(diag, monkeypatch):
    path = Path("/a")
    diag.install([("a", path)])
    monkeypatch.setattr(client._WATCH, "query", lambda *a, **kw: (1, (1,)))
    def unavailable(*args, **kwargs):
        raise RuntimeError("diagnostic sink failed")
    monkeypatch.setattr(diag, "_record", unavailable)
    assert client.outbox_pending([path]) is True


def test_map_replacement_discards_removed_waits(diag, monkeypatch):
    a, b = Path("/a"), Path("/b")
    monkeypatch.setattr(diag.time, "monotonic", lambda: 100)
    diag.install([("a", a)])
    _as("wait", diag.note, a, None)
    diag.install([("b", b)])
    _as("select", diag.note, a, None)
    rows = la.counters()
    assert rows["sync.probe_reuse_miss/slug/unmapped"]["runs"] == 1
    assert "sync.probe_wait_age_ms/slug/unmapped" not in rows
