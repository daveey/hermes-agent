"""Open/close compatibility and reservation order for tracked board observers."""
import contextlib
import gc
import os
import sqlite3
import threading
import weakref

import pytest

from hermes_cli import kanban_generation as generation
from hermes_cli import kanban_lockguard as guard
from hermes_cli import sqlite_safe_read as tracking
from hermes_cli.sqlite_safe_read import LiveConnectionError, has_live_connection, offline_file_access


@pytest.mark.parametrize('spelling', ['bytes', 'relative', 'uri', 'path'])
def test_filesystem_spelling_is_tracked(tmp_path, monkeypatch, spelling):
    target = tmp_path / 'board with space.db'
    with contextlib.closing(sqlite3.connect(target)) as conn:
        conn.execute('CREATE TABLE t(v)')
    monkeypatch.chdir(tmp_path)
    database, options = {
        'bytes': (os.fsencode(target), {}),
        'relative': (target.name, {}),
        'uri': (target.as_uri() + '?mode=ro', {'uri': True}),
        'path': (target, {}),
    }[spelling]
    with contextlib.closing(guard.connect(database, **options)) as conn:
        assert conn.execute('SELECT count(*) FROM t').fetchone() == (0,)
        assert has_live_connection(target)
        with pytest.raises(LiveConnectionError):
            with offline_file_access(target):
                pytest.fail('live reader admitted')
    assert not has_live_connection(target)


def test_legacy_positional_factory_and_duplicate_arguments(tmp_path):
    class Custom(sqlite3.Connection):
        pass

    target = tmp_path / 'board.db'
    with contextlib.closing(guard.connect(target, 0.0, 0, None, True, Custom)) as conn:
        assert isinstance(conn, Custom)
        assert conn.isolation_level is None
        assert has_live_connection(target)
    assert not has_live_connection(target)
    with pytest.raises(TypeError):
        guard.connect(target, 0.0, timeout=0.0)
    with pytest.raises(TypeError):
        guard.connect(target, *range(8))


@pytest.mark.parametrize('database,options', [
    (':memory:', {}), ('file::memory:', {'uri': True}),
    ('file:observer-memory?mode=memory&cache=shared', {'uri': True}),
])
def test_memory_connections_remain_usable(database, options):
    with contextlib.closing(guard.connect(database, **options)) as conn:
        assert conn.execute('SELECT 1').fetchone() == (1,)


def test_gc_releases_tracked_reader(tmp_path):
    target = tmp_path / 'board.db'
    conn = guard.connect(target)
    ref = weakref.ref(conn)
    assert has_live_connection(target)
    del conn
    gc.collect()
    assert ref() is None
    assert not has_live_connection(target)


@pytest.mark.platforms('linux')
def test_cross_thread_close_failure_retains_tracking(tmp_path):
    target = tmp_path / 'board.db'
    with contextlib.closing(sqlite3.connect(target)) as setup:
        setup.execute('PRAGMA journal_mode=WAL')
        setup.execute('CREATE TABLE t(v)')
    conn = guard.connect(target)
    assert guard.is_guarded(conn)
    errors = []
    counts = dict(guard._lockguard._HANDLES)

    def close():
        for _ in range(3):
            try:
                conn.close()
            except sqlite3.ProgrammingError as exc:
                errors.append(exc)

    thread = threading.Thread(target=close)
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    try:
        assert len(errors) == 3
        assert has_live_connection(target)
        assert guard.is_guarded(conn)
        assert dict(guard._lockguard._HANDLES) == counts
        assert conn.execute('SELECT 1').fetchone() == (1,)
    finally:
        conn.close()
    assert not has_live_connection(target)


def test_cross_thread_close_success_releases_tracking(tmp_path):
    target = tmp_path / 'board.db'
    conn = guard.connect(target, check_same_thread=False)
    thread = threading.Thread(target=conn.close)
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    assert not has_live_connection(target)


def test_reserved_open_is_busy_without_holding_board_over_retry(tmp_path, monkeypatch):
    target = tmp_path / 'board.db'
    with contextlib.closing(guard.connect(target)):
        pass
    observed = []
    original_retry = generation.retry_open

    def single_attempt(open_once, timeout):
        try:
            return open_once(0)
        finally:
            lock = generation.lifecycle_lock(target)
            acquired = []
            def probe():
                ok = lock.acquire(timeout=0)
                acquired.append(ok)
                if ok:
                    lock.release()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(3)
            assert not thread.is_alive()
            assert acquired == [True]
            observed.append(True)

    monkeypatch.setattr(generation, 'retry_open', single_attempt)
    with offline_file_access(target, what='test reservation'):
        with pytest.raises(sqlite3.OperationalError) as error:
            guard.connect(target, timeout=0)
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert not has_live_connection(target)
    assert observed == [True]
    monkeypatch.setattr(generation, 'retry_open', original_retry)
    with contextlib.closing(guard.connect(target, timeout=0)):
        assert has_live_connection(target)


def test_zero_budget_registry_contention_fails_closed(tmp_path):
    target = tmp_path / 'board.db'
    held, release = threading.Event(), threading.Event()

    def holder():
        with tracking._live_lock:
            held.set()
            assert release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(3)
        with pytest.raises(sqlite3.OperationalError) as error:
            guard.connect(target, timeout=0)
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert not target.exists()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
