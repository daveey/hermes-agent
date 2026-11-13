"""[daveey] Expired-but-unchanged clean verdicts are served while one pass refreshes them.

Under GIL load a libproc pass outran its 5 s budget, so every writable SessionDB open after each
30 s clean-verdict TTL blocked for the full 5 s (49 ``deleted-WAL holder scan timed out`` lines in
one hour on David-MBP, 2026-11-12). With the key unchanged since a clean pass nothing can have been
retired, so the open now returns that verdict at once and refreshes in the background; a changed
key, a verdict older than ``_DARWIN_FD_STALE_CLEAN_MAX_SECONDS`` or a holder still block as before.
libproc is faked with a slow pass, so these run on every platform.
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
import time

import pytest

import hermes_state_dbfile as dbfile
from hermes_state_dbfile import (
    _DARWIN_FD_DEV_OFFSET,
    _DARWIN_FD_INO_OFFSET,
    _DARWIN_FD_PATH_OFFSET,
    _DARWIN_FD_RECORD_SIZE,
    _iter_darwin_sidecar_holders,
)

SLOW_PASS = 2.0  # an injected slow scan, far below the 5 s budget
BUDGET = 5.0
GUARD = 30.0


@pytest.fixture(autouse=True)
def _fresh_scan_state(monkeypatch, settled_clean_keys):
    monkeypatch.setattr(dbfile, "_darwin_clean_until", {})
    monkeypatch.setattr(dbfile, "_DARWIN_SCAN_IN_FLIGHT", None)
    monkeypatch.setattr(dbfile, "_DARWIN_FD_SCAN_TIMEOUT_SECONDS", BUDGET)
    monkeypatch.setattr(dbfile, "_darwin_all_pids", lambda _lib: [4242])
    yield
    scan = dbfile._DARWIN_SCAN_IN_FLIGHT
    if scan is not None:
        scan.done.wait(GUARD)


class _SlowLibproc:
    """One pid whose fd listing takes ``delay``; with a ``target`` it holds that path at ``identity``."""

    def __init__(self, delay, target=None, identity=(0xDEAD, 0xBEEF)):
        self.delay = delay
        self.target = target
        self.identity = identity
        self.passes = 0

    def proc_pidinfo(self, pid, flavor, arg, buf, size):
        self.passes += 1
        threading.Event().wait(self.delay)  # health: allow HX013 -- the slow pass IS the behaviour under test
        if self.target is None:
            return 0  # no fds: a clean pass
        payload = struct.pack("<iI", 7, 1)
        ctypes.memmove(buf, payload, len(payload))
        return len(payload)

    def proc_pidfdinfo(self, pid, fd, flavor, record, size):
        blob = bytearray(_DARWIN_FD_RECORD_SIZE)
        struct.pack_into("<I", blob, _DARWIN_FD_DEV_OFFSET, self.identity[0])
        struct.pack_into("<Q", blob, _DARWIN_FD_INO_OFFSET, self.identity[1])
        encoded = self.target.encode("utf-8")
        blob[_DARWIN_FD_PATH_OFFSET:_DARWIN_FD_PATH_OFFSET + len(encoded)] = encoded
        ctypes.memmove(record, bytes(blob), len(blob))
        return 1


def _db_with_sidecars(tmp_path):
    db = tmp_path / "state.db"
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"state.db{suffix}").write_bytes(b"x")
    base = os.path.realpath(os.fspath(db))
    return db, base


def _plant_clean_verdict(base, *, age, key=None):
    """A clean verdict recorded ``age`` seconds ago whose 30 s TTL has passed."""
    now = time.monotonic()
    recorded = now - age
    dbfile._darwin_clean_until[base] = (
        min(recorded + dbfile._DARWIN_FD_CLEAN_TTL_SECONDS, now - 0.001),
        dbfile._darwin_clean_key(base) if key is None else key,
        recorded,
    )


def _timed(db):
    started = time.monotonic()
    result = _iter_darwin_sidecar_holders(db)
    return result, time.monotonic() - started


def _wait_refresh():
    scan = dbfile._DARWIN_SCAN_IN_FLIGHT
    assert scan is not None, "no refresh pass was started"
    assert scan.done.wait(GUARD)


def test_expired_same_key_verdict_returns_at_once_and_refreshes(monkeypatch, tmp_path):
    """(a) The open must not wait on the slow pass; the pass still runs and renews the verdict."""
    db, base = _db_with_sidecars(tmp_path)
    lib = _SlowLibproc(SLOW_PASS)
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: lib)
    _plant_clean_verdict(base, age=60)
    old_recorded = dbfile._darwin_clean_until[base][2]

    result, elapsed = _timed(db)

    assert result == []
    assert elapsed < 0.05, f"stale clean verdict blocked the open for {elapsed:.2f}s"
    _wait_refresh()
    assert lib.passes == 1
    until, key, recorded = dbfile._darwin_clean_until[base]
    assert key == dbfile._darwin_clean_key(base)
    assert recorded > old_recorded and until > time.monotonic(), "the refresh did not renew the verdict"


def test_changed_key_still_blocks_and_reports_a_holder(monkeypatch, tmp_path):
    """(b) A sidecar unlinked since the clean pass changes the key: the open waits for the pass
    and its positive verdict, so the refusal still happens."""
    db, base = _db_with_sidecars(tmp_path)
    _plant_clean_verdict(base, age=60)
    os.unlink(base + "-wal")  # a retired generation: some process still holds the old inode
    lib = _SlowLibproc(SLOW_PASS, target=base + "-wal")
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: lib)

    result, elapsed = _timed(db)

    assert result == [(4242, base + "-wal")]
    assert elapsed >= SLOW_PASS * 0.9, "a changed key must wait for the pass"
    assert base not in dbfile._darwin_clean_until


def test_positive_verdict_is_never_served_stale(monkeypatch, tmp_path):
    """(c) Even with an unchanged key, a refresh that finds a holder voids the clean verdict, so
    the next open blocks and refuses instead of being served the old ``[]``."""
    db, base = _db_with_sidecars(tmp_path)
    _plant_clean_verdict(base, age=60)
    lib = _SlowLibproc(0.2, target=base + "-wal")  # identity differs from the path's inode
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: lib)

    assert _iter_darwin_sidecar_holders(db) == []  # served stale while the refresh runs
    _wait_refresh()
    assert base not in dbfile._darwin_clean_until, "a positive refresh left the clean verdict cached"

    result, elapsed = _timed(db)
    assert result == [(4242, base + "-wal")]
    assert elapsed >= 0.2 * 0.9
    assert base not in dbfile._darwin_clean_until


def test_verdict_older_than_stale_bound_blocks(monkeypatch, tmp_path):
    """(d) Past ``_DARWIN_FD_STALE_CLEAN_MAX_SECONDS`` the open waits for a fresh pass."""
    db, base = _db_with_sidecars(tmp_path)
    assert dbfile._DARWIN_FD_STALE_CLEAN_MAX_SECONDS == 300
    _plant_clean_verdict(base, age=dbfile._DARWIN_FD_STALE_CLEAN_MAX_SECONDS + 1)
    lib = _SlowLibproc(SLOW_PASS)
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: lib)

    result, elapsed = _timed(db)

    assert result == []
    assert elapsed >= SLOW_PASS * 0.9, "a verdict past the stale bound must not be served"
    assert lib.passes == 1
    assert dbfile._darwin_clean_until[base][0] > time.monotonic()


def test_no_prior_verdict_blocks(monkeypatch, tmp_path):
    """No clean verdict at all keeps today's blocking join."""
    db, _base = _db_with_sidecars(tmp_path)
    lib = _SlowLibproc(SLOW_PASS)
    monkeypatch.setattr(dbfile, "_darwin_libproc", lambda: lib)

    result, elapsed = _timed(db)

    assert result == []
    assert elapsed >= SLOW_PASS * 0.9
