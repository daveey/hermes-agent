"""A host suspend must not hand a live holder's lapsed turn lease to the first waiter that wakes.

Incident (David-MBP, 2026-11-14 08:29:34 PST): a TUI turn held chat 20261026_170127_f07c32's lease, the
Mac slept ~7 min, the 300 s wall-clock TTL ran out during the sleep, and on the DarkWake the gateway's
notice turn reclaimed the row before the holder's refresher tick could renew it; the holder's finished
reply was then refused ("another Hermes process took over this session").
"""

from __future__ import annotations

import os
import time

import pytest

import hermes_state_wake_grace as wake_grace
from hermes_state import SessionDB


class _FakeClocks:
    """Drives the tracker's suspended-total and awake clocks."""

    def __init__(self, monkeypatch):
        self.suspended = 1000.0
        self.awake = 50_000.0
        monkeypatch.setattr(wake_grace, "suspended_total_s", lambda: self.suspended)
        monkeypatch.setattr(wake_grace, "_awake_now", lambda: self.awake)
        wake_grace._reset_for_tests()
        wake_grace.recently_woke()  # baseline sample before the "sleep"

    def sleep(self, seconds: float) -> None:
        self.suspended += seconds

    def run(self, seconds: float) -> None:
        self.awake += seconds


def _expire(db_path, holder):
    import sqlite3

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE session_turn_leases SET expires_at = ? WHERE holder = ?", (time.time() - 186, holder))
    conn.close()


def _owner(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT holder FROM session_turn_leases").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_waiter_does_not_steal_live_holders_lease_right_after_a_suspend(tmp_path, monkeypatch):
    clocks = _FakeClocks(monkeypatch)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=tui:platform=tui"
    waiter = f"pid={os.getpid()}:turn=gateway:platform=desktop"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)

    clocks.sleep(441)  # the Mac slept; the row's wall-clock TTL ran out meanwhile
    _expire(path, holder)

    assert not db.try_acquire_session_turn_lease("chat", waiter, ttl_seconds=300)
    assert _owner(path) == holder
    # The holder's post-wake refresher tick renews its own row (holder-fenced CAS).
    assert db.refresh_session_turn_lease("chat", holder, ttl_seconds=300)
    assert not db.try_acquire_session_turn_lease("chat", waiter, ttl_seconds=300)
    db.release_session_turn_lease("chat", holder)
    assert db.try_acquire_session_turn_lease("chat", waiter, ttl_seconds=300)


def test_live_holder_that_never_renews_is_reclaimed_after_the_wake_grace(tmp_path, monkeypatch):
    clocks = _FakeClocks(monkeypatch)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=wedged"
    waiter = f"pid={os.getpid()}:turn=next"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    clocks.sleep(600)
    _expire(path, holder)
    assert not db.try_acquire_session_turn_lease("chat", waiter, ttl_seconds=300)
    clocks.run(wake_grace.WAKE_GRACE_SECONDS + 1)
    assert db.try_acquire_session_turn_lease("chat", waiter, ttl_seconds=300)
    assert _owner(path) == waiter


def test_without_a_suspend_an_expired_lease_is_reclaimed_at_once(tmp_path, monkeypatch):
    _FakeClocks(monkeypatch)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=old"
    waiter = f"pid={os.getpid()}:turn=new"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    _expire(path, holder)
    assert db.try_acquire_session_turn_lease("chat", waiter, ttl_seconds=300)


def test_dead_holder_is_reclaimed_even_right_after_a_suspend(tmp_path, monkeypatch):
    clocks = _FakeClocks(monkeypatch)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    dead = "pid=999999:turn=gone"
    monkeypatch.setattr("hermes_state._compression_lock_holder_process_is_dead", lambda h: h == dead)
    assert db.try_acquire_session_turn_lease("chat", dead, ttl_seconds=300)
    clocks.sleep(600)
    _expire(path, dead)
    assert db.try_acquire_session_turn_lease("chat", f"pid={os.getpid()}:turn=new", ttl_seconds=300)


def test_runtime_worker_acquire_honors_the_wake_grace(tmp_path, monkeypatch):
    from hermes_state_runtime import _worker_turn

    clocks = _FakeClocks(monkeypatch)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=tui"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    clocks.sleep(441)
    _expire(path, holder)

    def acquire(holder_name):
        return db._execute_write(lambda conn: _worker_turn(
            db, conn, "chat", {"holder": holder_name, "ttl_seconds": 300}, "turn.acquire"))["value"]

    assert acquire(f"pid={os.getpid()}:turn=worker") is False
    assert _owner(path) == holder


def test_lease_less_writer_does_not_delete_live_holders_lease_right_after_a_suspend(tmp_path, monkeypatch):
    """Producer/detached writers check the turn lease without holding one; their stale rule must match
    acquisition, or the first post-wake delivery deletes the holder's row and the holder's refresh fails."""
    from hermes_state_errors import SessionTurnLeaseLostError

    clocks = _FakeClocks(monkeypatch)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=tui"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    clocks.sleep(441)
    _expire(path, holder)

    with pytest.raises(SessionTurnLeaseLostError):
        db.append_delegation_delivery("chat", "delegate answered", {"delegation_id": "d-1"})
    assert _owner(path) == holder
    assert db.refresh_session_turn_lease("chat", holder, ttl_seconds=300)


def test_lease_less_writer_reclaims_a_lapsed_lease_without_a_suspend(tmp_path, monkeypatch):
    _FakeClocks(monkeypatch)
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    holder = f"pid={os.getpid()}:turn=old"
    assert db.try_acquire_session_turn_lease("chat", holder, ttl_seconds=300)
    _expire(path, holder)
    db.append_delegation_delivery("chat", "delegate answered", {"delegation_id": "d-1"})
    assert _owner(path) is None


@pytest.mark.parametrize("value", [None])
def test_tracker_without_a_suspend_clock_never_defers(monkeypatch, value):
    monkeypatch.setattr(wake_grace, "suspended_total_s", lambda: value)
    wake_grace._reset_for_tests()
    assert wake_grace.recently_woke() is False
    assert wake_grace.recently_woke() is False
