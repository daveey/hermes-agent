"""A scan snapshot is not evidence that a descriptor is still held at verdict time."""
import os
import threading
import time

import pytest

import hermes_state_dbfile as dbfile


@pytest.mark.parametrize("current", [None, ("/other-wal", (3, 4)), ("/held-wal", (1, 2))])
def test_scan_revalidates_descriptors_before_publishing(tmp_path, monkeypatch, current):
    wal = str(tmp_path / "state.db-wal")
    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", lambda: iter([(42, 7, wal, (1, 2))]))
    monkeypatch.setattr(dbfile, "_darwin_fd_target", lambda pid, fd: current, raising=False)
    monkeypatch.setattr(dbfile, "_darwin_clean_until", {})
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)
    result = dbfile._iter_darwin_sidecar_holders(tmp_path / "state.db")
    assert result == ([(42, wal)] if current is not None and current[1] == (1, 2) else [])


def test_revalidation_is_bounded_and_does_not_cache_timeout(tmp_path, monkeypatch):
    gate = threading.Event()
    wal = str(tmp_path / "state.db-wal")
    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", lambda: iter([(42, 7, wal, (1, 2))]))
    monkeypatch.setattr(dbfile, "_darwin_clean_until", {})
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", 0.1)

    def revalidate(pid, fd):
        gate.wait(10)
        return wal, (1, 2)

    monkeypatch.setattr(dbfile, "_darwin_fd_target", revalidate)
    scan = None
    try:
        started = time.monotonic()
        assert dbfile._iter_darwin_sidecar_holders(tmp_path / "state.db") == []
        assert time.monotonic() - started < 5
        scan = dbfile._DARWIN_SCAN_IN_FLIGHT
        assert scan is not None
        assert not dbfile._darwin_clean_until
    finally:
        gate.set()
        if scan is not None:
            assert scan.done.wait(5)
    assert not dbfile._darwin_clean_until


@pytest.mark.platforms("macos")
def test_native_scan_does_not_refuse_after_normal_sidecar_close(tmp_path, monkeypatch):
    wal = tmp_path / "state.db-wal"
    wal.write_bytes(b"linked sidecar")
    held = wal.open("rb")
    original = dbfile._iter_darwin_fd_targets
    # This test exercises descriptor lifecycle, not loaded-host scan throughput.
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(dbfile, "_darwin_clean_until", {})
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)

    def enumerate_then_close():
        candidates = [row for row in original()
                      if row[0] == os.getpid() and row[1] == held.fileno()]
        assert candidates
        yield from candidates
        wal.unlink()
        held.close()

    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", enumerate_then_close)
    try:
        assert dbfile._iter_darwin_sidecar_holders(tmp_path / "state.db") == []
        assert dbfile._darwin_clean_until
    finally:
        held.close()


def test_descriptor_closing_during_path_comparison_is_not_a_holder(tmp_path, monkeypatch):
    wal = str(tmp_path / "state.db-wal")
    closed = False

    def compare(identity, path):
        nonlocal closed
        assert path == wal  # unrelated database paths must never be stat'ed
        closed = True
        return True

    monkeypatch.setattr(dbfile, "_identity_is_truly_unlinked", compare)
    monkeypatch.setattr(dbfile, "_darwin_fd_target", lambda pid, fd: None if closed else (wal, (1, 2)))
    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", lambda: iter([
        (42, 7, wal, (1, 2)), (43, 8, "/unrelated/state.db-wal", (3, 4))]))
    monkeypatch.setattr(dbfile, "_darwin_clean_until", {})
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)
    assert dbfile._iter_darwin_sidecar_holders(tmp_path / "state.db") == []
    assert closed
    assert dbfile._darwin_clean_until


def test_late_watcher_joins_while_verdict_is_computed_without_lock(tmp_path, monkeypatch):
    from tests.waiting import wait_until

    first = str(tmp_path / "first.db")
    second = str(tmp_path / "second.db")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def targets():
        calls.append("scan")
        yield 42, 7, first + "-wal", (1, 2)
        yield 42, 8, second + "-wal", (3, 4)

    def revalidate(pid, fd):
        assert dbfile._DARWIN_SCAN_LOCK.acquire(blocking=False)
        dbfile._DARWIN_SCAN_LOCK.release()
        if fd == 7:
            entered.set()
            assert release.wait(10)
            return first + "-wal", (1, 2)
        return second + "-wal", (3, 4)

    monkeypatch.setattr(dbfile, "_iter_darwin_fd_targets", targets)
    monkeypatch.setattr(dbfile, "_darwin_fd_target", revalidate)
    monkeypatch.setattr(dbfile, "_darwin_clean_until", {})
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", 30)
    results = {}
    threads = [threading.Thread(target=lambda path=path: results.update(
        {path: dbfile._iter_darwin_sidecar_holders(path)})) for path in (first, second)]
    try:
        threads[0].start()
        assert entered.wait(5)
        threads[1].start()
        wait_until(lambda: dbfile._DARWIN_SCAN_IN_FLIGHT.waiters == 2,
                   what="late watcher joins during first verdict")
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(10)
    assert results == {first: [(42, first + "-wal")], second: [(42, second + "-wal")]}
    assert calls == ["scan"]
