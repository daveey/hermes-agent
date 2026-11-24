"""One annotation pass spends one busy budget per board, not one per content-key read.

The content key is advisory (a failed read falls back to the file stat), so a busy board must not
make each key read wait out the reader's busy budget before the real read waits out its own.
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_reader_lifecycle as lifecycle
from hermes_cli import kanban_session_activity as activity
from hermes_cli.sqlite_safe_read import has_live_connection


@pytest.fixture
def busy_board(tmp_path, monkeypatch):
    activity._reset_cache_for_tests()
    board = tmp_path / "board.db"
    monkeypatch.setattr(activity, "_board_db_paths", lambda: [board])
    monkeypatch.setattr(activity, "_replica_paths", list)
    blocker = sqlite3.connect(board)
    blocker.execute("PRAGMA journal_mode=DELETE")
    blocker.execute("CREATE TABLE marker(value)")
    blocker.commit()
    yield board, blocker
    if blocker.in_transaction:
        blocker.rollback()
    blocker.close()
    activity._reset_cache_for_tests()


def _spy_budgets(monkeypatch):
    budgets = []
    original = lifecycle.cached_entry

    def spy(path, busy_budget=0.5):
        budgets.append(busy_budget)
        return original(path, busy_budget)

    monkeypatch.setattr(lifecycle, "cached_entry", spy)
    return budgets


def test_busy_board_counts_pass_waits_once(busy_board, monkeypatch):
    _board, blocker = busy_board
    budgets = _spy_budgets(monkeypatch)
    blocker.execute("BEGIN EXCLUSIVE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        activity._all_counts(include_snapshot=True)
    # Failed projection probe, board-memo key, then the real read. The probe answers a
    # locked board itself: falling back to another open used to make this 4 ([0, 0, 0, 0.5]).
    assert len(budgets) == 3
    assert [b for b in budgets if b > 0] == [0.5]
    assert budgets[-1] == 0.5


def test_busy_projection_probe_is_unknown_and_does_not_reopen(busy_board, monkeypatch):
    """A locked board is unknown from the projection open. A second open hides nothing and
    spends another busy budget (the CI failure on PR #4272, shard 8)."""
    board, blocker = busy_board
    budgets = _spy_budgets(monkeypatch)
    blocker.execute("BEGIN EXCLUSIVE")
    first = activity._projection_content_key(board)
    second = activity._projection_content_key(board)
    assert first != second
    assert first[0] == second[0] == "stat"
    assert budgets == [0, 0]


def test_uncovered_file_reuses_the_projection_open(tmp_path, monkeypatch):
    """A readable file with no projection column is the notify key, from that one open."""
    activity._reset_cache_for_tests()
    board = tmp_path / "old.db"
    conn = sqlite3.connect(board)
    conn.execute("CREATE TABLE marker(value)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(activity, "_board_db_paths", lambda: [board])
    monkeypatch.setattr(activity, "_replica_paths", list)
    budgets = _spy_budgets(monkeypatch)
    projected = activity._projection_content_key(board)
    assert budgets == [0]
    assert projected == activity._board_content_key(board)
    assert projected[0] != "projection"
    assert budgets == [0, 0]
    activity._reset_cache_for_tests()


def test_content_key_on_busy_board_falls_back_without_waiting(busy_board, monkeypatch):
    board, blocker = busy_board
    stat_key = ("stat", activity._revision.file_stat(board))
    sleeps = []
    from hermes_cli import kanban_generation

    monkeypatch.setattr(kanban_generation.time, "sleep", lambda s: sleeps.append(s))
    blocker.execute("BEGIN EXCLUSIVE")
    first = activity._board_content_key(board)
    second = activity._board_content_key(board)
    assert first[:2] == second[:2] == stat_key
    assert first != second  # a busy read is unknown, not proof of unchanged content
    assert sleeps == []  # no open retry loop behind a busy board
    assert not has_live_connection(board)


def test_cached_reader_statement_budget_restored_after_no_wait_key(busy_board):
    board, _blocker = busy_board
    with activity.cached_ro(board) as conn:  # warm: the key read reuses this cached handle
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 500
    seen = []
    with activity.cached_ro(board, busy_budget=0) as conn:
        seen.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    assert seen == [0]
    with activity.cached_ro(board) as again:
        assert again is conn
        assert again.execute("PRAGMA busy_timeout").fetchone()[0] == 500


def test_cached_reader_budget_restored_after_non_sqlite_error(busy_board):
    board, _blocker = busy_board
    with activity.cached_ro(board) as conn:
        pass
    with pytest.raises(RuntimeError), activity.cached_ro(board, busy_budget=0) as same:
        assert same is conn
        raise RuntimeError("body failed outside SQLite")
    with activity.cached_ro(board) as again:
        assert again is conn  # still cached: only sqlite errors evict
        assert again.execute("PRAGMA busy_timeout").fetchone()[0] == 500


def _during_hold(lock, call):
    """Release only after observing completion; old unbounded waits fail, not hang."""
    finished = threading.Event()

    def run():
        try:
            return call()
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock:
            future = pool.submit(run)
            returned_while_held = finished.wait(1)
        result = future.result(timeout=5)
    assert returned_while_held, "advisory read waited for another reader's Python lock"
    return result


def test_content_key_does_not_queue_behind_probe_lock(busy_board):
    board, _ = busy_board
    first = _during_hold(activity._revision.probe_lock(board), lambda: activity._board_content_key(board))
    second = _during_hold(activity._revision.probe_lock(board), lambda: activity._board_content_key(board))
    assert first[0] == second[0] == "stat"
    assert first != second  # contended probes cannot certify unchanged content
    assert activity._board_content_key(board)  # retry after contention still works


def test_zero_budget_content_key_does_not_queue_behind_borrowed_reader(busy_board):
    board, _ = busy_board
    with activity.cached_ro(board):
        pass
    lock = activity._RO_CACHE[str(board)][2]
    key = _during_hold(lock, lambda: activity._board_content_key(board))
    assert key[0] == "stat"
    with activity.cached_ro(board) as conn:
        assert conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0
