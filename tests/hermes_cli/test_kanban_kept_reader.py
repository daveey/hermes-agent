"""Kept read connections for Kanban board readers (``kanban_kept_reader``). [daveey]

Change-proportional design, card I: read callers share a process-wide pool of kept connections (at most
``MAX_PER_BOARD`` per board file, t_01a1db28414ae096), so
``kanban_generation.prepare_connection`` (and the OFD lock guard it applies) runs once per kept connection, and
the connection is reopened whenever the board file, its WAL generation or its init state changes under it.
"""
from __future__ import annotations

import contextlib
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


def test_recreated_shm_reopens_kept_connection(board):
    """A new or missing ``-shm`` is a new WAL generation (kanban-759104eb56): a kept connection still maps the
    deleted one and must be reopened, although the board file itself (inode, init state) did not change."""
    from hermes_cli.kanban_lockguard import shm_identity

    with kept.reading() as conn:
        old = conn
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    key = gen._resolved(str(board))
    assert getattr(old, "_hermes_generation_shm", None) == shm_identity(key) is not None
    os.unlink(str(board) + "-shm")  # the idle kept connection now maps a deleted WAL index
    with kept.reading() as conn:
        assert conn is not old
        assert getattr(conn, "_hermes_generation_shm", None) == shm_identity(key) is not None
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert _is_closed(old)


def test_recovery_closed_connection_is_not_reused(board):
    """Generation recovery closes a stale handle with ``conn.close()``, which only ``forget``s it in
    ``kanban_generation``; the kept entry is not told. The next borrow must notice and reopen."""
    from hermes_cli.kanban_lockguard import shm_identity

    # Keep the WAL generation alive: otherwise last-close removes -shm and its
    # independent identity check masks a missing recovery-closed check.
    with kbc.connect_closing(board):
        with kept.reading() as conn:
            old = conn
        key = gen._resolved(str(board))
        identity = shm_identity(key)
        assert identity is not None
        old.close()  # recovery's close/forget boundary, without retiring the kept entry
        assert old not in gen._connections
        assert shm_identity(key) == identity
        assert key in kbc._INITIALIZED_PATHS and key not in gen._poisoned
        with kept.reading() as conn:
            assert conn is not old
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_connection_left_in_transaction_is_not_kept_and_next_read_sees_new_write(board):
    """A body that leaves a read transaction open must not leave that connection kept: its snapshot would serve
    every later read stale."""
    with kept.reading() as conn:
        first = conn
        conn.execute("BEGIN")
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0  # the snapshot starts here
    assert _is_closed(first)
    tid = _triage("written after the open snapshot")
    with kept.reading() as conn:
        assert conn is not first
        assert [r["id"] for r in conn.execute("SELECT id FROM tasks")] == [tid]


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


def _boards(base: Path, n: int) -> list[Path]:
    paths = []
    for i in range(n):
        path = base.parent / f"extra-{i}.db"
        kb.init_db(path)
        paths.append(path)
    return paths


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def _kept_keys() -> list[str]:
    """Board keys of the pool's open entries, least recently borrowed first."""
    return [e.key for e in list(kept._registry) if not e.closed]


def test_cap_eviction_skips_a_borrowed_entry_and_closes_the_idle_one(board, monkeypatch):
    """Opening ``MAX_KEPT`` other boards inside a borrow of board A must not evict A (the least recently borrowed,
    but borrowed): the cap closes the least recently borrowed IDLE entry and keeps A."""
    monkeypatch.setattr(kept, "MAX_KEPT", 4)
    others = _boards(board, kept.MAX_KEPT)
    key_a = gen._resolved(str(board))
    with kept.reading(board) as a:
        opened = []
        for path in others:
            with kept.reading(path) as conn:
                opened.append(conn)
        assert key_a in _kept_keys(), "a borrowed entry was evicted from the pool"
        assert a.execute("SELECT 1").fetchone()[0] == 1
    assert _is_closed(opened[0]), "the evicted idle connection was left to GC instead of closed"
    assert all(not _is_closed(c) for c in opened[1:])
    assert len(_kept_keys()) == kept.MAX_KEPT
    with kept.reading(board) as again:
        assert again is a  # still kept, reachable by close_all/recovery
    assert kept.close_all() == kept.MAX_KEPT
    assert _is_closed(a)


def test_cap_overflow_with_every_entry_borrowed_closes_the_evicted_one_on_exit(board, monkeypatch):
    """Every kept entry borrowed (nested borrows of MAX_KEPT + 1 boards): the newest is not kept, and its
    connection is closed when its own borrow exits, not leaked; no borrowed entry is closed under its borrower."""
    monkeypatch.setattr(kept, "MAX_KEPT", 4)
    others = _boards(board, kept.MAX_KEPT)
    with contextlib.ExitStack() as stack:
        a = stack.enter_context(kept.reading(board))
        conns = [stack.enter_context(kept.reading(path)) for path in others]
        assert len(_kept_keys()) == kept.MAX_KEPT
        assert gen._resolved(str(others[-1])) not in _kept_keys()
        assert gen._resolved(str(board)) in _kept_keys()
        assert all(c.execute("SELECT 1").fetchone()[0] == 1 for c in (a, *conns))  # never closed under a borrower
    assert _is_closed(conns[-1])
    assert not _is_closed(a)
    assert len(_kept_keys()) == kept.MAX_KEPT


def test_sqlite_error_in_body_drops_the_connection(board):
    with pytest.raises(sqlite3.OperationalError):
        with kept.reading() as conn:
            first = conn
            conn.execute("SELECT * FROM no_such_table")
    with kept.reading() as conn:
        assert conn is not first


def test_exited_thread_connections_stay_in_the_pool_and_close_all_closes_them(board):
    """A kept connection outlives the thread that opened it (it belongs to the pool), and ``close_all`` on another
    thread closes it."""
    got: dict = {}

    def other():
        with kept.reading() as conn:
            got["conn"] = conn

    worker = threading.Thread(target=other)
    worker.start()
    worker.join(10)
    import gc
    gc.collect()
    with kept.reading() as conn:
        assert conn is got["conn"]
    assert kept.close_all() == 1
    with pytest.raises(sqlite3.ProgrammingError):
        got["conn"].execute("SELECT 1")


def test_a_failed_kept_open_releases_its_board_slot(board, monkeypatch):
    """An open that raises must give its ``MAX_PER_BOARD`` slot back, or the board degrades to per-call opens."""
    real = kept._open

    def failing(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kept, "_open", failing)
    for _ in range(kept.MAX_PER_BOARD):
        with pytest.raises(sqlite3.OperationalError):
            with kept.reading():
                pass
    monkeypatch.setattr(kept, "_open", real)
    assert kept._opening == {}
    opens = kept.stats["opens"]
    with kept.reading() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert kept.stats["opens"] == opens + 1


def test_an_unkeepable_open_releases_its_board_slot(board, monkeypatch):
    """A fenced or read-only-schema open is per-call; its ``MAX_PER_BOARD`` slot must come back."""
    real = kept._open
    plain: list = []

    def unkeepable(path, key, board_slug):
        conn = kept._plain(path, board_slug)
        plain.append(conn)
        return conn, None

    monkeypatch.setattr(kept, "_open", unkeepable)
    for _ in range(kept.MAX_PER_BOARD):
        with kept.reading() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert all(_is_closed(c) for c in plain)
    monkeypatch.setattr(kept, "_open", real)
    assert kept._opening == {}
    opens = kept.stats["opens"]
    with kept.reading() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert kept.stats["opens"] == opens + 1


def test_an_open_in_flight_counts_toward_max_per_board(board, monkeypatch):
    """Two threads reaching an empty board at once must not both keep a connection past MAX_PER_BOARD."""
    monkeypatch.setattr(kept, "MAX_PER_BOARD", 1)
    real = kept._open
    entered, release = threading.Event(), threading.Event()

    def slow(*a, **kw):
        entered.set()
        assert release.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(kept, "_open", slow)

    def first():
        with kept.reading() as conn:
            conn.execute("SELECT 1")

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(10)
        monkeypatch.setattr(kept, "_open", real)
        fallbacks = kept.stats["fallbacks"]
        with kept.reading() as conn:  # the first open is still in flight: this borrow is per-call
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert kept.stats["fallbacks"] == fallbacks + 1
        assert _is_closed(conn)
    finally:
        release.set()
        worker.join(10)
    assert len(_kept_keys()) == 1
