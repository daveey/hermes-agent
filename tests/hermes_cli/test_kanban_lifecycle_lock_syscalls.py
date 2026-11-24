"""Cached board reads must not hold the board lifecycle lock across avoidable syscalls (t_01a1ce5c1d7bfe42).

On the hub serve (~130 threads) every GIL-releasing syscall under the board lock waits out the 5 ms switch interval
behind busy threads; per-read ``realpath`` and a clean ``/proc/self/fd`` rescan were the top lock holders left after
the fd-scan fix, and readers that wait past their 0.5 s budget log "busy; reused last counts".
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time

import pytest

from hermes_cli import kanban_generation as gen


@pytest.fixture(autouse=True)
def _fresh_caches():
    for name in ("_resolved_paths", "_clean_scans"):
        getattr(gen, name, {}).clear()
    yield
    for name in ("_resolved_paths", "_clean_scans"):
        getattr(gen, name, {}).clear()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_board_path_resolves_once_per_ttl_and_again_after(monkeypatch, tmp_path):
    clock = _Clock()
    calls = []
    real = gen._resolve_now
    monkeypatch.setattr(gen, "_clock", clock)
    monkeypatch.setattr(gen, "_resolve_now", lambda v: calls.append(v) or real(v))
    db = str(tmp_path / "kanban.db")

    first = gen._path(db)
    assert calls == [db]
    for _ in range(50):
        assert gen._path(db) == first
    assert calls == [db]                        # reused: no resolve at all

    clock.t += gen._RESOLVE_TTL_S + 0.1
    assert gen._path(db) == first
    assert calls == [db, db]                    # resolved again after the TTL


def test_retargeted_symlink_is_seen_after_the_ttl(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gen, "_clock", clock)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    link = tmp_path / "board"
    link.symlink_to(a)
    assert gen._path(str(link / "kanban.db")) == str(a / "kanban.db")
    link.unlink(); link.symlink_to(b)
    clock.t += gen._RESOLVE_TTL_S + 0.1
    assert gen._path(str(link / "kanban.db")) == str(b / "kanban.db")


def test_clean_sidecar_scan_is_reused_only_while_shm_identity_is_unchanged(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(gen, "_clock", clock)
    scans = []
    scan = lambda p: scans.append(p) or []

    assert gen._stranded("/b/kanban.db", (1, 10), scan) == []
    assert gen._stranded("/b/kanban.db", (1, 10), scan) == []
    assert len(scans) == 1                      # clean verdict reused
    assert gen._stranded("/b/kanban.db", (1, 11), scan) == []
    assert len(scans) == 2                      # -shm replaced: rescan at once
    assert gen._stranded("/b/kanban.db", None, scan) == []
    assert len(scans) == 3                      # -shm deleted: rescan at once
    clock.t += gen._CLEAN_SCAN_TTL_S + 0.1
    assert gen._stranded("/b/kanban.db", None, scan) == []
    assert len(scans) == 4                      # TTL expired


def test_a_positive_sidecar_scan_is_never_reused(monkeypatch):
    monkeypatch.setattr(gen, "_clock", _Clock())
    results = [["/b/kanban.db-wal (deleted)"], ["/b/kanban.db-wal (deleted)"], []]
    scan = lambda p: results.pop(0)
    assert gen._stranded("/b/kanban.db", (1, 10), scan)
    assert gen._stranded("/b/kanban.db", (1, 10), scan)
    assert gen._stranded("/b/kanban.db", (1, 10), scan) == []


def test_relative_board_path_is_resolved_against_the_current_directory_every_time(monkeypatch, tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    monkeypatch.chdir(tmp_path / "a")
    assert gen._path("kanban.db") == str(tmp_path / "a" / "kanban.db")
    monkeypatch.chdir(tmp_path / "b")
    assert gen._path("kanban.db") == str(tmp_path / "b" / "kanban.db")


def test_a_replaced_wal_alone_forces_a_rescan(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "_clock", _Clock())
    db = str(tmp_path / "kanban.db")
    wal = tmp_path / "kanban.db-wal"
    wal.write_bytes(b"x")
    keep = open(wal, "rb")                      # hold the old inode so the replacement gets a new one
    scans = []
    scan = lambda p: scans.append(p) or []
    gen._stranded(db, (1, 10), scan)
    gen._stranded(db, (1, 10), scan)
    assert len(scans) == 1
    wal.unlink(); wal.write_bytes(b"y")         # -shm identity unchanged, -wal replaced
    gen._stranded(db, (1, 10), scan)
    assert len(scans) == 2
    keep.close()


def _wal_board(tmp_path):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table t(x)")
    conn.commit()
    return db, conn


def test_recover_reuses_a_clean_scan_and_rescans_a_poisoned_board(monkeypatch, tmp_path):
    from hermes_cli import kanban_lockguard

    monkeypatch.setattr(gen, "_clock", _Clock())
    db, conn = _wal_board(tmp_path)
    calls = []
    monkeypatch.setattr(kanban_lockguard, "stranded_sidecars", lambda p: calls.append(p) or [])
    try:
        for _ in range(5):
            gen.recover(str(db))
        assert len(calls) == 1                  # no registered handle: scanned once, then reused
        with gen._lock:
            gen._poisoned.add(gen._path(str(db)))
        gen.recover(str(db))
        assert len(calls) == 2                  # poisoned: always rescans
    finally:
        with gen._lock:
            gen._poisoned.discard(gen._path(str(db)))
        conn.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc fd scan is Linux-only")
def test_recover_without_handles_does_not_rescan_fds_while_threads_are_cpu_bound(monkeypatch, tmp_path):
    """Steady-state cached reads of a clean board run no ``/proc/self/fd`` scan under the board lock.

    Before the fix every read rescanned (~7 s for 600 fds behind 4 busy threads, against a 0.5 s reader budget).
    Asserted as a call count, not a wall-clock bound, so CPU contention can't make it flaky.
    """
    from hermes_cli import kanban_lockguard

    monkeypatch.setattr(gen, "_clock", _Clock())  # frozen: the 5 s clean-scan TTL can't expire under load
    db, conn = _wal_board(tmp_path)
    real_scan = kanban_lockguard.stranded_sidecars
    calls = []
    monkeypatch.setattr(kanban_lockguard, "stranded_sidecars", lambda p: calls.append(p) or real_scan(p))
    stop = threading.Event()
    running = [threading.Event() for _ in range(4)]

    def spin(started):
        started.set()
        while not stop.is_set():
            pass

    spinners = [threading.Thread(target=spin, args=(ev,), daemon=True) for ev in running]
    try:
        gen.recover(str(db))                    # first call scans once and records a clean verdict
        assert len(calls) == 1
        for t in spinners:
            t.start()
        assert all(ev.wait(timeout=5) for ev in running)
        for _ in range(10):
            gen.recover(str(db))
        assert len(calls) == 1, f"recover rescanned fds {len(calls) - 1} times while the board was clean"
    finally:
        stop.set()
        for t in spinners:
            t.join()
        conn.close()
