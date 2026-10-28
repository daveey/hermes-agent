"""A kernel-blocked vnode must not wedge Hermes startup (#113187).

``_iter_darwin_fd_targets`` calls ``proc_pidfdinfo`` for every descriptor of
every process. If any process holds a descriptor whose vnode lookup blocks in
the kernel (e.g. uninterruptible I/O on a dead network share), that call never
returns -- and it used to run synchronously on the SessionDB connect path, so
one stuck unrelated process froze every Hermes start indefinitely.

``_iter_darwin_sidecar_holders`` must therefore bound the whole pass with a
wall-clock deadline (a per-iteration check cannot fire while blocked inside a
single call) and fail open with a warning. libproc is faked throughout, so
these run on every platform.
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import threading
import time

import hermes_state_dbfile as dbfile
from hermes_state_dbfile import (
    _DARWIN_FD_INO_OFFSET,
    _DARWIN_FD_PATH_OFFSET,
    _DARWIN_FD_DEV_OFFSET,
    _DARWIN_FD_RECORD_SIZE,
    _iter_darwin_sidecar_holders,
)

import pytest


@pytest.fixture(autouse=True)
def _fresh_scan_cache(monkeypatch):
    dbfile._darwin_clean_until.clear()
    # A hung fake scan never leaves the in-flight slot; the next test must not join it.
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)
    yield
    dbfile._darwin_clean_until.clear()


# Far below the 5s production budget so the suite stays fast; far above zero
# so scheduling jitter cannot flake it.
BUDGET = 2.0
# Guard so a regression (scan blocks again) fails instead of hanging the suite.
HANG_GUARD = 30.0


class _HungLibproc:
    """One pid, one fd, whose vnode lookup never returns (state-U process)."""

    def proc_pidinfo(self, pid, flavor, arg, buf, size):
        payload = struct.pack("<iI", 7, 1)
        ctypes.memmove(buf, payload, len(payload))
        return len(payload)

    def proc_pidfdinfo(self, pid, fd, flavor, record, size):
        threading.Event().wait()
        return 0  # unreachable


class _HealthyLibproc:
    """One pid, one vnode fd naming ``target`` with the given identity."""

    def __init__(self, target, identity):
        self._target = target
        self._identity = identity

    def proc_pidinfo(self, pid, flavor, arg, buf, size):
        payload = struct.pack("<iI", 7, 1)
        ctypes.memmove(buf, payload, len(payload))
        return len(payload)

    def proc_pidfdinfo(self, pid, fd, flavor, record, size):
        blob = bytearray(_DARWIN_FD_RECORD_SIZE)
        struct.pack_into("<I", blob, _DARWIN_FD_DEV_OFFSET, self._identity[0])
        struct.pack_into("<Q", blob, _DARWIN_FD_INO_OFFSET, self._identity[1])
        encoded = self._target.encode("utf-8")
        blob[_DARWIN_FD_PATH_OFFSET:_DARWIN_FD_PATH_OFFSET + len(encoded)] = encoded
        ctypes.memmove(record, bytes(blob), len(blob))
        return 1


def _run_guarded(func, *args):
    """Run ``func`` so a hang fails the test instead of hanging the suite."""
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(func(*args)), daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(HANG_GUARD)
    return worker, time.monotonic() - started, outcome


def test_hung_vnode_scan_fails_open_within_budget(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: _HungLibproc())
    monkeypatch.setattr(dbfile, "_darwin_all_pids", lambda _lib: [4242])
    if hasattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS"):
        monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", BUDGET)
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        worker, elapsed, outcome = _run_guarded(
            _iter_darwin_sidecar_holders, tmp_path / "state.db"
        )
    assert not worker.is_alive(), "scan blocked past the hang guard: no deadline"
    assert elapsed < HANG_GUARD
    assert outcome == [[]], "a timed-out scan must fail open (no holders, no refusal)"
    assert any(
        "timed out" in record.getMessage() for record in caplog.records
    ), "deadline expiry must log a visible warning"


def test_healthy_scan_still_reports_holder(monkeypatch, tmp_path, caplog):
    """The timeout wrapper must not swallow a fast, successful scan."""
    watched = os.path.realpath(os.path.join(os.fspath(tmp_path), "state.db-wal"))
    # The watched path names nothing: stat fails, so the fd's identity is
    # judged a retired generation and the holder is reported.
    lib = _HealthyLibproc(watched, (0xDEAD, 0xBEEF))
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: lib)
    monkeypatch.setattr(dbfile, "_darwin_all_pids", lambda _lib: [4242])
    if hasattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS"):
        monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", BUDGET)
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        worker, elapsed, outcome = _run_guarded(
            _iter_darwin_sidecar_holders, tmp_path / "state.db"
        )
    assert not worker.is_alive()
    assert elapsed < BUDGET, "a fast scan must not wait out the budget"
    assert outcome == [[(4242, watched)]]
    assert not any(
        "timed out" in record.getMessage() for record in caplog.records
    ), "a successful scan must not log a timeout warning"


def test_clean_verdict_is_reused_and_scans_are_single_flight(monkeypatch, tmp_path):
    """Every SessionDB open used to walk all fds of all processes; a clean pass is now cached."""
    calls = []

    class _Empty:
        def proc_pidinfo(self, *a):
            calls.append(1)
            return 0

    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: _Empty())
    monkeypatch.setattr(dbfile, "_darwin_all_pids", lambda _lib: [4242])
    db = tmp_path / "state.db"
    results = []
    threads = [threading.Thread(target=lambda: results.append(_iter_darwin_sidecar_holders(db)))
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join(HANG_GUARD) for t in threads]
    assert results == [[]] * 8
    assert len(calls) == 1, "concurrent/cached opens must share one libproc pass"
    dbfile._darwin_clean_until.clear()
    _iter_darwin_sidecar_holders(db)
    assert len(calls) == 2


def test_caller_arriving_after_pass_but_before_cache_write_reuses_it(monkeypatch, tmp_path):
    """A caller that missed the cache and then found no pass in flight used to start another
    pass, because the finished pass's clean verdict was only cached after its waiters woke. Here
    the second caller arrives in exactly that gap, so the old code made two passes every time."""
    calls = []

    class _Empty:
        def proc_pidinfo(self, *a):
            calls.append(1)
            return 0

    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: _Empty())
    monkeypatch.setattr(dbfile, "_darwin_all_pids", lambda _lib: [4242])
    db = tmp_path / "state.db"
    real_join = dbfile._join_darwin_fd_scan
    nested = []

    def join_then_late_caller(*args, **kwargs):
        scan = real_join(*args, **kwargs)
        if not nested:  # the pass is done and out of flight; the first caller has not cached yet
            nested.append(_iter_darwin_sidecar_holders(db))
        return scan

    monkeypatch.setattr(dbfile, "_join_darwin_fd_scan", join_then_late_caller)
    assert _iter_darwin_sidecar_holders(db) == []
    assert nested == [[]]
    assert len(calls) == 1, "a caller arriving after the pass must reuse its clean verdict"


def test_timed_out_scan_is_not_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: _HungLibproc())
    monkeypatch.setattr(dbfile, "_darwin_all_pids", lambda _lib: [4242])
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", 0.3)
    assert _iter_darwin_sidecar_holders(tmp_path / "state.db") == []
    assert not dbfile._darwin_clean_until


@pytest.mark.parametrize("stub_start", ["inline", "never"])
def test_thread_stubs_never_capture_the_scan_pass(monkeypatch, tmp_path, stub_start):
    """Tests replace threading.Thread with stubs whose start() runs the target inline or never (93 call
    sites). The pass must stay on its own real thread under both. Run inline, it re-took _DARWIN_SCAN_LOCK
    on the thread already holding it and deadlocked: all of test_tui_gateway_server.py hung on macOS from
    10-27. Never started, the SessionDB open waited out the scan timeout and the stub recorded the pass as
    the code under test's thread (test_interrupt_before_agent_ready_prevents_late_turn_start saw 2)."""
    watched = os.path.realpath(os.path.join(os.fspath(tmp_path), "state.db-wal"))
    lib = _HealthyLibproc(watched, (0xDEAD, 0xBEEF))
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: lib)
    monkeypatch.setattr(dbfile, "_darwin_all_pids", lambda _lib: [4242])
    # Private copies, restored at teardown: a regression then wedges only this test's lock and slot.
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_LOCK", type(dbfile._DARWIN_SCAN_LOCK)())
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)
    real_thread = threading.Thread
    captured = []

    class _StubThread:
        def __init__(self, target=None, **_kwargs):
            self._target = target
            captured.append(getattr(target, "__qualname__", repr(target)))

        def start(self):
            if stub_start == "inline":
                self._target()

    monkeypatch.setattr(threading, "Thread", _StubThread)
    # This module is often first imported while such a stub is installed: what it would bind then.
    assert dbfile._interpreter_thread_class() is real_thread
    outcome = []
    worker = real_thread(
        target=lambda: outcome.append(_iter_darwin_sidecar_holders(tmp_path / "state.db")), daemon=True)
    worker.start()
    worker.join(10.0)
    assert not worker.is_alive(), "the pass deadlocked on _DARWIN_SCAN_LOCK"
    assert captured == [], "a threading.Thread stub captured the scan pass"
    assert outcome == [[(4242, watched)]]
    assert dbfile._DARWIN_SCAN_IN_FLIGHT is None
