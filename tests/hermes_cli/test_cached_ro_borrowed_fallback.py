"""A borrowed shared reader never fails a concurrent ``cached_ro`` call: the caller gets a private connection.

Hub 2026-11-21: concurrent dashboard readers (session-panel feed, /session-cards, sidebar) raised
"database is locked: cached reader is borrowed" 165 times in one errors.log, flipping status panels to unknown.
"""

import contextlib
import sqlite3
import threading

import pytest

from hermes_cli import kanban_session_activity as activity


@pytest.fixture
def board(tmp_path):
    activity._reset_cache_for_tests()
    path = tmp_path / "board.db"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", [("a",), ("b",)])
        conn.commit()
    yield path
    activity._reset_cache_for_tests()


def _shared_entry(path):
    return activity._RO_CACHE[str(path)]


@contextlib.contextmanager
def _held_by_other_thread(path):
    """Thread A holds ``cached_ro(path)`` open until the block exits; yields A's connection."""
    holding, release = threading.Event(), threading.Event()
    seen = []

    def hold():
        with activity.cached_ro(path) as conn:
            seen.append(conn)
            holding.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert holding.wait(10)
        yield seen[0]
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()


def _in_thread(call):
    result, errors = [], []

    def run():
        try:
            result.append(call())
        except BaseException as exc:  # surfaced to the test thread below
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    if errors:
        raise errors[0]
    return result[0]


def _enter_conn(path):
    with activity.cached_ro(path) as conn:
        return conn


def test_borrowed_reader_falls_back_to_a_private_connection(board):
    with _held_by_other_thread(board) as shared:
        entry = _shared_entry(board)

        def read():
            with activity.cached_ro(board) as conn:
                return conn, sorted(r[0] for r in conn.execute("SELECT v FROM t"))

        private, rows = _in_thread(read)
        assert private is not shared
        assert rows == ["a", "b"]
        assert _shared_entry(board) is entry  # the fallback never touches the shared cache entry
        assert entry[0] is shared


def test_zero_budget_borrowed_reader_also_falls_back(board):
    with _held_by_other_thread(board) as shared:
        def read():
            with activity.cached_ro(board, busy_budget=0) as conn:
                return conn, conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]

        private, count = _in_thread(read)
        assert private is not shared
        assert count == 2


def _is_closed(conn):
    """Only meaningful on the thread that owns ``conn`` (or a ``check_same_thread=False`` one): a foreign-thread call
    raises ProgrammingError for an open connection too."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        assert "thread" not in str(exc), exc
        return True
    return False


def test_fallback_connection_is_closed_after_the_body(board):
    with _held_by_other_thread(board):
        def read():
            with activity.cached_ro(board) as conn:
                assert not _is_closed(conn)
            return _is_closed(conn)  # checked on the owning thread

        assert _in_thread(read) is True


def test_fallback_connection_is_closed_when_the_body_raises(board):
    with _held_by_other_thread(board) as shared:
        entry = _shared_entry(board)
        captured = []

        def read():
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                with activity.cached_ro(board) as conn:
                    captured.append(conn)
                    conn.execute("SELECT * FROM no_such_table")
            return _is_closed(captured[0])  # checked on the owning thread

        assert _in_thread(read) is True
        assert captured[0] is not shared
        assert _shared_entry(board) is entry  # a fallback error does not evict the shared reader
        assert not _is_closed(shared)


def test_shared_entry_is_kept_and_reused_after_contention(board):
    with _held_by_other_thread(board) as shared:
        entry = _shared_entry(board)
        assert _in_thread(lambda: _enter_conn(board)) is not shared
    assert _shared_entry(board) is entry
    with activity.cached_ro(board) as conn:
        assert conn is entry[0]
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_uncontended_calls_reuse_the_cached_connection(board):
    with activity.cached_ro(board) as first:
        pass
    with activity.cached_ro(board) as second:
        assert second.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    assert first is second is _shared_entry(board)[0]
    assert not _is_closed(first)
    assert _in_thread(lambda: _enter_conn(board)) is first  # another thread, uncontended: same shared reader


def test_shared_lock_is_released_after_the_body(board):
    with activity.cached_ro(board):
        pass
    lock = _shared_entry(board)[2]
    assert lock.acquire(blocking=False)
    lock.release()


def test_fallback_is_read_only_and_counted(board):
    before = activity.ro_stats["private_fallbacks"]
    with _held_by_other_thread(board):
        def write():
            with activity.cached_ro(board) as conn:
                conn.execute("INSERT INTO t VALUES ('c')")

        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|query_only"):
            _in_thread(write)
    assert activity.ro_stats["private_fallbacks"] == before + 1
    with activity.cached_ro(board) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_fallback_busy_timeout_is_the_remaining_budget(board):
    with _held_by_other_thread(board):
        def timeout_ms():
            with activity.cached_ro(board, busy_budget=0.4) as conn:
                return conn.execute("PRAGMA busy_timeout").fetchone()[0]

        ms = _in_thread(timeout_ms)
    # 400 ms budget minus the <=50 ms shared wait and the entry lookup; never the reader's full 500 ms.
    assert 150 <= ms <= 360


def test_holder_releasing_within_the_wait_hands_over_the_shared_reader(board, monkeypatch):
    monkeypatch.setattr(activity, "_RO_SHARED_WAIT_S", 5)  # a late timer thread on a loaded runner is not a fallback
    with activity.cached_ro(board):
        pass
    entry = _shared_entry(board)
    lock = entry[2]
    before = activity.ro_stats["private_fallbacks"]
    assert lock.acquire(blocking=False)
    timer = threading.Timer(0.01, lock.release)
    timer.start()
    try:
        with activity.cached_ro(board, busy_budget=5) as conn:  # the wait is min(budget, _RO_SHARED_WAIT_S)
            assert conn is entry[0]
    finally:
        timer.join(5)
    assert activity.ro_stats["private_fallbacks"] == before


def test_missing_board_still_raises(tmp_path):
    activity._reset_cache_for_tests()
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        with activity.cached_ro(tmp_path / "absent.db"):
            pass


def test_borrow_lock_keeps_its_raising_contract():
    from hermes_cli.kanban_reader_lifecycle import borrow_lock, try_borrow

    lock = threading.Lock()
    with lock:
        assert try_borrow(lock, 0) is False
        with pytest.raises(sqlite3.OperationalError, match="cached reader is borrowed"):
            with borrow_lock(lock, 0):
                pass
    with borrow_lock(lock, 0):
        assert lock.locked()
    assert not lock.locked()


def test_fallback_busy_timeout_never_exceeds_the_shared_readers(board):
    with _held_by_other_thread(board):
        def timeout_ms():
            with activity.cached_ro(board, busy_budget=5) as conn:
                return conn.execute("PRAGMA busy_timeout").fetchone()[0]

        assert _in_thread(timeout_ms) == int(activity._RO_BUSY_TIMEOUT_S * 1000)


def test_fallback_opens_through_the_cache_opener(board, monkeypatch):
    opened = []
    real = activity._open_ro

    def spy(path, *args, **kwargs):
        conn = real(path, *args, **kwargs)
        opened.append(conn)
        return conn

    with activity.cached_ro(board):
        pass
    monkeypatch.setattr(activity, "_open_ro", spy)
    with _held_by_other_thread(board):
        private = _in_thread(lambda: _enter_conn(board))
    assert opened == [private]
