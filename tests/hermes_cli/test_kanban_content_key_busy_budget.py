"""One annotation pass spends one busy budget per board, not one per content-key read.

The content key is advisory (a failed read falls back to the file stat), so a busy board must not
make each key read wait out the reader's busy budget before the real read waits out its own.
"""

import sqlite3

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
    # Fingerprint key, board-memo key, then the real read: only the read may wait.
    assert len(budgets) == 3
    assert [b for b in budgets if b > 0] == [0.5]
    assert budgets[-1] == 0.5


def test_content_key_on_busy_board_falls_back_without_waiting(busy_board, monkeypatch):
    board, blocker = busy_board
    stat_key = ("stat", activity._revision.file_stat(board))
    sleeps = []
    from hermes_cli import kanban_generation

    monkeypatch.setattr(kanban_generation.time, "sleep", lambda s: sleeps.append(s))
    blocker.execute("BEGIN EXCLUSIVE")
    assert activity._board_content_key(board) == stat_key
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


def test_warm_key_read_on_busy_board_does_not_wait_in_sqlite(busy_board):
    board, blocker = busy_board
    with activity.cached_ro(board) as conn:
        conn.execute("SELECT count(*) FROM marker").fetchone()
    blocker.execute("BEGIN EXCLUSIVE")
    # The cached handle is reused; with busy_timeout=0 SQLite raises at once and the key falls back.
    assert activity._board_content_key(board)[0] == "stat"
    blocker.rollback()
    with activity.cached_ro(board) as reopened:
        assert reopened.execute("PRAGMA busy_timeout").fetchone()[0] == 500
