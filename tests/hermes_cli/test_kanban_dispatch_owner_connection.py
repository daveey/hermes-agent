"""Dispatch must reject handles invalidated by native board lifecycle operations."""
from contextlib import closing
from hermes_cli import kanban_db_connect as kbc
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_dispatch import DispatchResult
from hermes_cli.kanban_dispatch_owner import run_owned_tick, set_dispatch_owner


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.create_board("sample")
    path = kb.board_dir("sample") / "kanban.db"
    set_dispatch_owner(path, "dedicated")
    return path


@pytest.mark.parametrize("archive", [True, False])
def test_stale_connection_cannot_dispatch_recreated_board(board, archive):
    with closing(kbc.connect(board="sample")) as conn:
        kb.remove_board("sample", archive=archive)
        kb.create_board("sample")
        callback = Mock(return_value=DispatchResult())
        result = run_owned_tick(conn, callback, None, "sample", False)
        assert result.skipped_owned
        callback.assert_not_called()
    with closing(kbc.connect(board="sample")) as fresh:
        callback = Mock(return_value=DispatchResult())
        assert not run_owned_tick(fresh, callback, None, "sample", False).skipped_owned
        callback.assert_called_once()


def test_gateway_checks_identity_after_lock_acquisition(board, monkeypatch):
    from contextlib import contextmanager
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher
    from hermes_cli import kanban_db_connect as kbc

    original = kbc._dispatch_tick_lock
    replaced = False

    @contextmanager
    def interleave(path, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            kb.remove_board("sample")
            kb.create_board("sample")
        with original(path, **kwargs) as held:
            yield held

    dispatcher = _KanbanDispatcher.__new__(_KanbanDispatcher)
    # Arrange the native lifecycle transition after opening, before gating.
    with closing(kbc.connect(board="sample")) as conn:
        @contextmanager
        def existing(**kwargs):
            yield conn
        monkeypatch.setattr(kbc, "connect_closing", existing)
        monkeypatch.setattr(kbc, "_dispatch_tick_lock", interleave)
        with dispatcher._unowned_board_work("sample") as allowed:
            assert allowed is None


def test_contended_open_requires_reconnect_before_dispatch(board):
    with kbc._dispatch_tick_lock(board) as held:
        assert held
        conn = kbc.connect(board)
    with closing(conn):
        callback = Mock(return_value=DispatchResult())
        result = run_owned_tick(conn, callback, "dedicated", "sample", False)
        assert result.skipped_locked
        assert not result.skipped_owned and not result.ownership_error
        callback.assert_not_called()
    with closing(kbc.connect(board)) as fresh:
        callback = Mock(return_value=DispatchResult())
        result = run_owned_tick(fresh, callback, "dedicated", "sample", False)
        assert not result.skipped_owned
        assert result.dispatch_owner == "dedicated"
        callback.assert_called_once()


def test_raw_connection_requires_native_reconnect(board):
    import sqlite3
    with closing(sqlite3.connect(board)) as conn:
        callback = Mock(return_value=DispatchResult())
        result = run_owned_tick(conn, callback, "dedicated", "sample", False)
        assert result.skipped_owned and result.ownership_error
        callback.assert_not_called()


def test_moved_lock_inode_is_not_authority(board, monkeypatch):
    original = kbc._try_lock_nb
    moved = board.with_name("old-lock")
    def relocate_before_acquire(handle):
        lock = board.with_name(board.name + ".dispatch.lock")
        lock.rename(moved)
        lock.touch()
        return original(handle)
    monkeypatch.setattr(kbc, "_try_lock_nb", relocate_before_acquire)
    with kbc._dispatch_tick_lock(board) as held:
        assert not held
