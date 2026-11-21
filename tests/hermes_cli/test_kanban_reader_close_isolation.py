"""A reader must finish cleanup while a native board opener retries a busy journal."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_lockguard as guard
from hermes_cli import kanban_session_activity as activity
from hermes_cli.kanban_dispatch_owner import connect_with_identity
from hermes_cli.kanban_generation import open_ro


@pytest.mark.parametrize("reader", ["cached", "plain"])
def test_reader_close_does_not_wait_for_busy_native_opener(tmp_path, monkeypatch, reader):
    path = tmp_path / "board.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE marker(value)")
    seed.commit()
    seed.close()
    have_conn, go, native_setup = threading.Event(), threading.Event(), threading.Event()
    original = guard._apply

    def observed(conn, *args, **kwargs):
        if threading.current_thread().name.startswith("native-open"):
            native_setup.set()
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(guard, "_apply", observed)

    def read():
        if reader == "cached":
            with activity.cached_ro(path) as conn:
                have_conn.set()
                assert go.wait(10)
                conn.execute("SELECT count(*) FROM marker").fetchone()
        else:
            conn = open_ro(path)
            try:
                have_conn.set()
                assert go.wait(10)
                conn.execute("SELECT count(*) FROM marker").fetchone()
            finally:
                conn.close()

    def native():
        conn = connect_with_identity(path, timeout=30)
        conn.close()

    blocker = sqlite3.connect(path)
    try:
        with ThreadPoolExecutor(max_workers=1) as reads, ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="native-open") as opens:
            pending = reads.submit(read)
            try:
                assert have_conn.wait(10)
                blocker.execute("BEGIN EXCLUSIVE")
                opening = opens.submit(native)
                assert native_setup.wait(10)
                start = time.monotonic()
                go.set()
                with pytest.raises(sqlite3.OperationalError):
                    pending.result(timeout=3)
                assert time.monotonic() - start < 3
                assert not opening.done()
            finally:
                go.set()
                blocker.rollback()
            opening.result(timeout=5)
    finally:
        blocker.close()
        activity._reset_cache_for_tests()


def test_tracked_reader_close_serializes_with_registry_without_deadlock(tmp_path, monkeypatch):
    from hermes_cli import sqlite_safe_read

    path = tmp_path / "board.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE marker(value)")
    seed.commit()
    seed.close()
    entered = threading.Event()
    release = threading.Event()
    ready = threading.Event()
    closing = threading.Event()
    original = sqlite_safe_read.connect_tracked

    def observed(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite_safe_read, "connect_tracked", observed)

    def read():
        conn = open_ro(path)
        ready.set()
        assert release.wait(10)
        closing.set()
        conn.close()

    def native():
        conn = connect_with_identity(path, timeout=30)
        conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(read)
        assert ready.wait(10)
        entered.clear()
        try:
            # Pin registry contention explicitly: unrelated raw I/O no longer
            # owns this mutex. Keep the blocked-open assertion below unchanged.
            with sqlite_safe_read._live_lock:
                opening = pool.submit(native)
                assert entered.wait(10)
                # Readers now register their fd lifetime too: real close and
                # unregister must serialize under this mutex, not escape it.
                release.set()
                assert closing.wait(3)
                assert not reading.done()
                assert not opening.done()
            reading.result(timeout=3)
        finally:
            release.set()
        opening.result(timeout=5)


@pytest.mark.parametrize("connect", [guard.connect, connect_with_identity])
def test_setup_retry_expires_and_restores_sql_budget(tmp_path, connect):
    from hermes_cli.sqlite_safe_read import has_live_connection

    path = tmp_path / "board.db"
    blocker = sqlite3.connect(path)
    try:
        blocker.execute("CREATE TABLE marker(value)")
        blocker.commit()
        blocker.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            connect(path, timeout=0.1)
        assert time.monotonic() - start < 2
        assert not has_live_connection(path)
        blocker.rollback()
        conn = connect(path, timeout=0.1)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 100
            assert conn.execute("SELECT count(*) FROM marker").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        blocker.close()
