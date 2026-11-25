"""A warm read is not an open/close transition; retain cold admission fences."""
import contextlib
import sqlite3
import threading

import pytest

from hermes_cli import kanban_generation as generation
from hermes_cli import kanban_session_activity as activity


@pytest.fixture
def board(tmp_path):
    activity._reset_cache_for_tests()
    path = tmp_path / "board.db"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t(v)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    yield path
    activity._reset_cache_for_tests()


@contextlib.contextmanager
def held_elsewhere(lock):
    holding, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            holding.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert holding.wait(10)
        yield
    finally:
        release.set()
        thread.join(10)
        assert not thread.is_alive()


def test_warm_zero_budget_read_during_other_lifecycle_transition(board):
    with activity.cached_ro(board) as conn:
        shared = conn
    with held_elsewhere(generation.lifecycle_lock(board)):
        with activity.cached_ro(board, busy_budget=0) as conn:
            assert conn is shared
            assert conn.execute("SELECT v FROM t").fetchall() == [(1,)]
    # The next borrow sees a real commit without a TTL or explicit cache kick.
    with contextlib.closing(sqlite3.connect(board)) as writer:
        writer.execute("UPDATE t SET v=2")
        writer.commit()
    with activity.cached_ro(board, busy_budget=0) as conn:
        assert conn.execute("SELECT v FROM t").fetchall() == [(2,)]


def test_cold_zero_budget_read_keeps_lifecycle_admission(board):
    with held_elsewhere(generation.lifecycle_lock(board)):
        with pytest.raises(generation.BoardLifecycleBusy):
            with activity.cached_ro(board, busy_budget=0):
                pytest.fail("a cold open entered during a lifecycle transition")


def assert_cold_admission(board):
    from hermes_cli import kanban_reader_lifecycle as readers

    with held_elsewhere(generation.lifecycle_lock(board)):
        assert readers.borrow_warm(board) is None
        with pytest.raises(generation.BoardLifecycleBusy):
            with activity.cached_ro(board, busy_budget=0):
                pytest.fail("uncertain retained custody bypassed ordinary admission")
    # Every declined probe released borrower custody, including early returns.
    assert activity._RO_CACHE[str(board)][2].acquire(blocking=False)
    activity._RO_CACHE[str(board)][2].release()


@pytest.mark.parametrize("invalid", ["poison", "forgotten", "tracking", "shm"])
def test_uncertain_warm_entry_keeps_lifecycle_admission(board, monkeypatch, invalid):
    import weakref

    with activity.cached_ro(board) as shared:
        pass
    invalidators = {
        "poison": lambda: monkeypatch.setattr(generation, "_poisoned", {shared._hermes_generation_path}),
        "forgotten": lambda: monkeypatch.setattr(generation, "_connections", weakref.WeakSet()),
        "tracking": lambda: monkeypatch.setattr(shared, "_hermes_tracked_path", None),
        "shm": lambda: monkeypatch.setattr("hermes_cli.kanban_lockguard.shm_identity", lambda path: (-1, -1)),
    }
    invalidators[invalid]()
    assert_cold_admission(board)


def test_transaction_declines_warm_admission(board):
    with activity.cached_ro(board) as shared:
        pass
    shared.execute("BEGIN")
    try:
        assert_cold_admission(board)
    finally:
        shared.rollback()


def test_stale_sibling_declines_warm_admission(board, monkeypatch):
    with activity.cached_ro(board):
        pass
    with contextlib.closing(generation.open_ro(board, check_same_thread=False)) as sibling:
        monkeypatch.setattr(sibling, "_hermes_generation_shm", (-1, -1))
        assert_cold_admission(board)


def test_replaced_main_file_declines_warm_admission(board, tmp_path):
    from hermes_cli import kanban_reader_lifecycle as readers

    with activity.cached_ro(board):
        pass
    replacement = tmp_path / "replacement.db"
    with contextlib.closing(sqlite3.connect(replacement)) as conn:
        conn.execute("CREATE TABLE t(v)")
        conn.execute("INSERT INTO t VALUES (99)")
        conn.commit()
    replacement.replace(board)
    with held_elsewhere(generation.lifecycle_lock(board)):
        assert readers.borrow_warm(board) is None
        with pytest.raises(generation.BoardLifecycleBusy):
            with activity.cached_ro(board, busy_budget=0):
                pytest.fail("main-file replacement bypassed recovery")
    with activity.cached_ro(board) as conn:
        assert conn.execute("SELECT v FROM t").fetchone() == (99,)


def test_error_cleanup_retains_tracking_until_lifecycle_owner_releases(board):
    from hermes_cli import sqlite_safe_read as safe

    with activity.cached_ro(board):
        pass
    entered, finished = threading.Event(), threading.Event()
    errors = []

    def fail_read():
        try:
            with activity.cached_ro(board, busy_budget=0):
                entered.set()
                raise sqlite3.OperationalError("fixture body failed")
        except sqlite3.OperationalError as exc:
            errors.append(str(exc))
        finally:
            finished.set()

    thread = threading.Thread(target=fail_read)
    try:
        with held_elsewhere(generation.lifecycle_lock(board)):
            thread.start()
            assert entered.wait(10)
            assert not finished.is_set()  # close still owes lifecycle custody
            # A failed read cannot untrack its descriptor before the actual close.
            assert safe.has_live_connection(board)
        thread.join(10)
        assert not thread.is_alive()
        assert errors == ["fixture body failed"]
        assert finished.is_set()
        assert not safe.has_live_connection(board)
        assert str(board) not in activity._RO_CACHE
    finally:
        thread.join(10)


def test_warm_custody_excludes_raw_probe_without_acquiring_tracking_mutex(board):
    from hermes_cli import sqlite_safe_read as safe

    with activity.cached_ro(board):
        pass
    with held_elsewhere(safe._live_lock):
        with activity.cached_ro(board, busy_budget=0) as conn:
            assert conn.execute("SELECT v FROM t").fetchone() == (1,)
    with pytest.raises(safe.LiveConnectionError):
        with safe.offline_file_access(board, what="test probe"):
            pytest.fail("a retained connection must still exclude raw access")
