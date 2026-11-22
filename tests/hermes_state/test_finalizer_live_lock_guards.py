"""Behavioral controls for finalizer scope and failed deferred closes."""

import sqlite3
import threading

import pytest

from hermes_cli import sqlite_safe_read as safe


@pytest.mark.parametrize("scope", ["exited_on_closer", "active_on_other_thread"])
def test_explicit_close_waits_outside_its_own_finalizer_scope(tmp_path, monkeypatch, scope):
    conn = safe.connect_tracked(tmp_path / "scope.db", check_same_thread=False)
    real_lock = safe._live_lock
    attempted, finished = threading.Event(), threading.Event()
    modes, errors = [], []

    class ObservedLock:
        def acquire(self, blocking=True):
            modes.append(blocking)
            attempted.set()
            return real_lock.acquire(blocking=blocking)

        def release(self):
            real_lock.release()

    def close():
        try:
            if scope == "exited_on_closer":
                with safe.finalizer_close():
                    pass
            conn.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    runner = threading.Thread(target=close, daemon=True)
    with monkeypatch.context() as patch:
        patch.setattr(safe, "_live_lock", ObservedLock())
        with safe.finalizer_close():
            # This thread owns the real lock while the closer attempts acquisition.
            # An active finalizer on this thread must not affect the closer.
            with real_lock:
                runner.start()
                assert attempted.wait(10)
                assert modes == [True], "explicit close inherited a finalizer scope"
                assert not finished.is_set(), "explicit close bypassed the held lock"
            assert finished.wait(10)
        runner.join(10)
    assert not runner.is_alive()
    assert errors == []
    assert not safe.has_live_connection(tmp_path / "scope.db")


def test_cross_thread_deferred_close_failure_keeps_path_live(tmp_path):
    path = tmp_path / "thread-affine.db"
    conn = safe.connect_tracked(path)  # SQLite's real check_same_thread=True default.
    held, release = threading.Event(), threading.Event()

    def hold():
        with safe._live_lock:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(10)
    try:
        with safe.finalizer_close():
            conn.close()
    finally:
        release.set()
        holder.join(10)

    observations, errors = [], []

    def drain():
        try:
            observations.append(safe.has_live_connection(path))
        except Exception as exc:
            errors.append(exc)

    runner = threading.Thread(target=drain, daemon=True)
    runner.start()
    runner.join(10)
    try:
        assert not runner.is_alive()
        assert errors == [], "failed cross-thread close escaped the deferred drain"
        assert observations == [True], "failed close must not unregister an open connection"
        assert conn.execute("SELECT 1").fetchone() == (1,)
        assert safe.has_live_connection(path)
        with pytest.raises(safe.LiveConnectionError):
            with safe.offline_file_access(path):
                pass
    finally:
        conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert not safe.has_live_connection(path)
