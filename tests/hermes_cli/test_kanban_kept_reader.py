"""Kept read connections for Kanban board readers (``kanban_kept_reader``). [daveey]

Change-proportional design, card I: a read caller keeps one connection per (thread, board file), so
``kanban_generation.prepare_connection`` (and the OFD lock guard it applies) runs once per kept connection, and
the connection is reopened whenever the board file, its WAL generation or its init state changes under it.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_generation as gen
from hermes_cli import kanban_kept_reader as kept


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    path = kb.init_db()
    yield Path(path)
    kept.close_all()


@pytest.fixture
def prepares(monkeypatch):
    calls: list = []
    real = gen.prepare_connection

    def counting(conn, timeout):
        calls.append(conn)
        return real(conn, timeout)

    monkeypatch.setattr(gen, "prepare_connection", counting)
    return calls


def _triage(title: str) -> str:
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee="default", triage=True)


def test_list_triage_ids_prepares_one_connection_across_passes(board, prepares):
    """RED on main: every ``list_triage_ids`` (each auto-decompose pass, per board) opened and prepared a
    fresh connection."""
    from hermes_cli import kanban_decompose as decomp

    tid = _triage("needs a spec")
    prepares.clear()
    for _ in range(5):
        assert decomp.list_triage_ids() == [tid]
    assert len(prepares) == 1


def test_kept_reader_sees_later_writes_and_refuses_its_own(board):
    with kept.reading() as conn:
        first = conn
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kanban_notify_subs DEFAULT VALUES")
    tid = _triage("written by another connection")
    with kept.reading() as conn:
        assert conn is first
        assert [r["id"] for r in conn.execute("SELECT id FROM tasks")] == [tid]


def _replace_board(path: Path, title: str) -> None:
    """Swap a different board file in under the same path (a restore): new inode, its own sidecars."""
    copy = path.with_name("restored.db")
    with kbc.connect_closing(path) as src, sqlite3.connect(copy) as dst:
        src.backup(dst)
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the old -wal must hold no frames for the new file
    dst = sqlite3.connect(copy, isolation_level=None)
    try:
        dst.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_restored', ?, 'triage', 0)",
                    (title,))
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(str(copy) + suffix)
        except FileNotFoundError:
            pass
    os.replace(copy, path)


def test_file_replaced_under_kept_connection_reopens(board, prepares):
    with kept.reading() as conn:
        old = conn
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    _replace_board(board, "from the restored file")
    prepares.clear()
    with kept.reading() as conn:
        assert conn is not old
        assert [r["id"] for r in conn.execute("SELECT id FROM tasks")] == ["t_restored"]
    assert len(prepares) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        old.execute("SELECT 1")  # the stale handle was closed, not left holding the old inode


def test_dropped_init_state_reopens_through_connect(board, prepares):
    """``init_db``/``repair_db``/a lost schema drop the path from ``_INITIALIZED_PATHS``; the next read reopens
    through ``connect``'s init path instead of trusting a connection opened before."""
    with kept.reading() as conn:
        old = conn
    with kbc._INIT_LOCK:
        kbc._INITIALIZED_PATHS.discard(str(board.resolve()))
    with kept.reading() as conn:
        assert conn is not old
    assert str(board.resolve()) in kbc._INITIALIZED_PATHS


def test_idle_kept_connection_on_another_thread_is_evicted_by_recovery(board):
    """Generation recovery must be able to retire an idle kept connection parked on another thread; otherwise
    every other thread's open of a stranded board fails until that thread reads again."""
    got: dict = {}
    parked, resume, done = threading.Event(), threading.Event(), threading.Event()

    def other():
        with kept.reading() as conn:
            got["first"] = conn
        parked.set()
        resume.wait(10)
        with kept.reading() as conn:
            got["second"] = conn
            got["rows"] = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        kept.close_all()
        done.set()

    worker = threading.Thread(target=other)
    worker.start()
    assert parked.wait(10)
    kept._evict(gen._resolved(str(board)))  # what kanban_generation.recover calls before closing stale handles
    with pytest.raises(sqlite3.ProgrammingError):
        got["first"].execute("SELECT 1")
    resume.set()
    worker.join(10)
    assert done.is_set() and got["second"] is not got["first"] and got["rows"] == 0


def test_borrowed_kept_connection_is_never_closed_under_its_borrower(board):
    with kept.reading() as conn:
        with pytest.raises(gen.DeletedWalGenerationError):
            kept._evict(gen._resolved(str(board)))
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_nested_borrow_gets_its_own_connection(board):
    with kept.reading() as outer:
        with kept.reading() as inner:
            assert inner is not outer
            assert inner.execute("SELECT 1").fetchone()[0] == 1
        assert outer.execute("SELECT 1").fetchone()[0] == 1
    with kept.reading() as again:
        assert again is outer


def test_sqlite_error_in_body_drops_the_connection(board):
    with pytest.raises(sqlite3.OperationalError):
        with kept.reading() as conn:
            first = conn
            conn.execute("SELECT * FROM no_such_table")
    with kept.reading() as conn:
        assert conn is not first


def test_exited_thread_connections_are_closed_by_the_next_borrow(board):
    got: dict = {}

    def other():
        with kept.reading() as conn:
            got["conn"] = conn

    worker = threading.Thread(target=other)
    worker.start()
    worker.join(10)
    import gc
    gc.collect()
    with kept.reading():
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        got["conn"].execute("SELECT 1")
