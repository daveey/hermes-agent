"""Deterministic open-scope snapshot boundary regressions. [daveey]"""

from types import SimpleNamespace

import pytest

from hermes_cli import loop_accounting as la


@pytest.fixture
def cpu_clock(monkeypatch):
    la.reset()
    clock = [1.0]
    tid = la._native_id()
    monkeypatch.setattr(la, "_thread_time", lambda: clock[0])
    monkeypatch.setattr(la.os, "listdir", lambda _path: [])
    monkeypatch.setattr(la.sys, "platform", "darwin")  # os-marker: ok — synthetic clock, not Darwin ABI coverage
    monkeypatch.setattr(la, "_darwin_thread_cpu", lambda: [(tid, "pool_0", clock[0])])
    monkeypatch.setattr(la.os, "times", lambda: SimpleNamespace(user=clock[0], system=0.0))
    monkeypatch.setattr(la, "_process_start_time", lambda: 1.0)
    monkeypatch.setattr(la, "_role_guess", lambda: "test")
    monkeypatch.setitem(la._thread_targets, tid, "concurrent.futures.thread._worker")
    yield clock
    la.reset()


def _pool(snap):
    return snap["residual"]["targets"]["concurrent.futures.thread._worker"]


def test_scope_open_at_start_does_not_make_interval_residual_negative(cpu_clock):
    with la.scope("thread/worker"):
        cpu_clock[0] = 1.08
        start = la.snapshot()
        cpu_clock[0] = 1.10
    end = la.snapshot()
    before, after = _pool(start), _pool(end)
    used = after["cpu_s"] - before["cpu_s"]
    accounted = after["accounted_cpu_s"] - before["accounted_cpu_s"]
    assert used - accounted == pytest.approx(0.0, abs=1e-6)
    assert before["accounted_cpu_s"] == pytest.approx(0.08)
    assert after["accounted_cpu_s"] == pytest.approx(0.10)


def test_open_nested_scope_and_pass_union_is_counted_once(cpu_clock):
    with la.scope("thread/worker"):
        cpu_clock[0] = 1.02
        with la.scope("sync.round/loop"):
            cpu_clock[0] = 1.03
            with la.pass_("sync.board/default"):
                cpu_clock[0] = 1.08
                inside = la.snapshot()
            after_pass = la.snapshot()
        after_round = la.snapshot()
    closed = la.snapshot()
    for snap in (inside, after_pass, after_round, closed):
        assert _pool(snap)["accounted_cpu_s"] == pytest.approx(0.08)
        assert snap["process"]["accounted_cpu_s"] == pytest.approx(0.08)


@pytest.mark.parametrize("kind", ["pass", "span", "scope"])
def test_snapshot_overlay_does_not_book_or_repeat_cpu(cpu_clock, kind):
    observed = la.pass_("open") if kind == "pass" else la.scope("open")
    token = la.span_open("open") if kind == "span" else observed.__enter__()
    try:
        cpu_clock[0] = 1.04
        for _ in range(2):
            snap = la.snapshot()
            assert _pool(snap)["accounted_cpu_s"] == pytest.approx(0.04)
            assert "open" not in snap["loops"]
            row = next(t for t in snap["threads"] if t["tid"] == la._native_id())
            assert row["open_accounting"]["name"] == "open"
            assert not la._thread_accounted
    finally:
        if kind == "span":
            assert la.span_close(token)
        else:
            observed.__exit__(None, None, None)
    assert la.counters()["open"]["cpu_s"] == pytest.approx(0.04)
    assert not la._open_intervals.by_tid


def test_unscoped_gap_is_not_absorbed_by_next_scope(cpu_clock):
    with la.scope("first"):
        cpu_clock[0] = 1.04
    cpu_clock[0] = 1.08
    with la.scope("second"):
        cpu_clock[0] = 1.10
        snap = la.snapshot()
    assert _pool(snap)["accounted_cpu_s"] == pytest.approx(0.06)
    assert la.counters()["first"]["cpu_s"] == pytest.approx(0.04)
    assert la.counters()["second"]["cpu_s"] == pytest.approx(0.02)


def test_scope_inside_outer_pass_counts_union_once(cpu_clock):
    with la.pass_("outer"):
        cpu_clock[0] = 1.02
        with la.scope("inner"):
            cpu_clock[0] = 1.08
            inside = la.snapshot()
        middle = la.snapshot()
    closed = la.snapshot()
    for snap in (inside, middle, closed):
        assert _pool(snap)["accounted_cpu_s"] == pytest.approx(0.08)


def test_child_open_scope_uses_child_clock_and_discards_foreign_threads(cpu_clock):
    with la.scope("inherited"):
        cpu_clock[0] = 1.08
        la._open_intervals.enter(-123, object(), "foreign", 0.0, 0.0)
        cpu_clock[0] = 0.005
        la._after_fork_child()
        assert -123 not in la._open_intervals.by_tid
        cpu_clock[0] = 0.015
        inside = la.snapshot()
        assert inside["process"]["accounted_cpu_s"] == pytest.approx(0.01)
    assert la.counters()["inherited"]["cpu_s"] == pytest.approx(0.01)
    assert not la._open_intervals.by_tid


@pytest.mark.parametrize("sample", ["missing", "imprecise", "overbooked"])
def test_incomplete_open_boundary_is_explicit_and_does_not_erase_booked_cpu(cpu_clock, monkeypatch, sample):
    with la.scope("thread/incomplete"):
        cpu_clock[0] = 1.08
        if sample == "missing":
            monkeypatch.setattr(la, "_darwin_thread_cpu", list)
        elif sample == "imprecise":
            monkeypatch.setattr(la, "_thread_cpu_locked", lambda: [{
                "tid": la._native_id(), "name": "pool_0", "cpu_s": 1.08,
                "accounted_cpu_s": 0.0, "cpu_sample_precise": False,
            }])
        else:
            la.record("invalid.caller", cpu_s=0.2)
        snap = la.snapshot()
        assert snap["open_accounting_complete"] is False
        if sample == "overbooked":
            assert snap["process"]["accounted_cpu_s"] == pytest.approx(0.2)


def test_complete_open_boundary_is_explicit(cpu_clock):
    with la.scope("thread/complete"):
        cpu_clock[0] = 1.08
        assert la.snapshot()["open_accounting_complete"] is True
