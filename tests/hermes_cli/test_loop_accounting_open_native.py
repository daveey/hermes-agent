"""Real thread clocks and publication/close serialization. [daveey]"""
from __future__ import annotations

import threading
import time

import pytest

from hermes_cli import loop_accounting as la


@pytest.fixture(autouse=True)
def fresh():
    la.reset()
    yield
    la.reset()


def burn(seconds):
    until = time.thread_time() + seconds
    while time.thread_time() < until:
        pass


@pytest.mark.platforms("linux", "macos")
def test_open_native_thread_interval_includes_only_post_snapshot_cpu():
    ready, advance, finished, release = (threading.Event() for _ in range(4))

    def worker():
        with la.scope("thread/native_boundary"):
            burn(0.08)
            ready.set()
            assert advance.wait(10)
            burn(0.02)
        finished.set()
        assert release.wait(10)

    thread = threading.Thread(target=worker, name="boundary-pool_0")
    thread.start()
    try:
        assert ready.wait(10)
        start = next(row for row in la.thread_cpu() if row["tid"] == thread.native_id)
        advance.set()
        assert finished.wait(10)
        end = next(row for row in la.thread_cpu() if row["tid"] == thread.native_id)
        assert start["accounted_cpu_s"] >= 0.075
        used = end["cpu_s"] - start["cpu_s"]
        booked = end["accounted_cpu_s"] - start["accounted_cpu_s"]
        # Event/lock release after closing is deliberately unscoped. This is a
        # native-clock correctness tolerance, not the live collector's policy.
        assert -0.00001 <= used - booked < 0.01
    finally:
        advance.set()
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not la._open_intervals.by_tid


def test_native_sampling_serializes_scope_close(monkeypatch):
    ready, close_go, attempted, closed = (threading.Event() for _ in range(4))

    def worker():
        with la.scope("thread/closing"):
            ready.set()
            assert close_go.wait(10)
            attempted.set()
        closed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(10)
    original = la._thread_cpu_locked

    def sample_while_closing():
        close_go.set()
        assert attempted.wait(10)
        assert not closed.wait(0.05), "scope closed while its native snapshot was in flight"
        return original()

    monkeypatch.setattr(la, "_thread_cpu_locked", sample_while_closing)
    try:
        la.thread_cpu()
    finally:
        close_go.set()
        thread.join(10)
    assert closed.is_set()
    assert not thread.is_alive()
