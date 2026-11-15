"""``kanban_kept_reader`` keeps a process-wide pool, at most ``MAX_PER_BOARD`` connections per board file. [daveey]

The gateway's auto-decompose tick reads every board from whichever ``hermes-gateway-default`` executor thread runs
it, and those threads never exit. Kept per (thread, board), the reader ended up holding executor threads x boards
connections (daveey-mini 2026-11-14: 12 threads x 4 boards, ~0.9 MB each, t_01a1db28414ae096). Kept per board, one
or two serve every thread.

Each test here is RED on the per-thread reader. The constants are read with defaults so the old module fails on
behaviour, not on a missing name.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_generation as gen
from hermes_cli import kanban_kept_reader as kept

MAX_PER_BOARD = getattr(kept, "MAX_PER_BOARD", 2)
MAX_KEPT = getattr(kept, "MAX_KEPT", 32)


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    kept.close_all()
    yield tmp_path / "boards"
    kept.close_all()


def _boards(base: Path, n: int) -> list[Path]:
    base.mkdir(exist_ok=True)
    paths = []
    for i in range(n):
        path = base / f"board-{i}.db"
        kb.init_db(path)
        paths.append(path)
    return paths


def _live() -> list:
    """Kept entries that still hold an open connection (both the per-thread and the pool reader register here)."""
    return [e for e in list(kept._registry) if not e.closed]


def _per_board() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in _live():
        counts[entry.key] = counts.get(entry.key, 0) + 1
    return counts


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_executor_threads_share_one_kept_connection_per_board(home):
    """a. 12 live threads (the gateway executor) each read 4 boards in turn: the per-thread reader kept 48."""
    boards = _boards(home, 4)
    turn = threading.Lock()  # one tick at a time, as the dispatcher runs it
    finished = threading.Barrier(13)
    errors: list[BaseException] = []

    def tick():
        try:
            with turn:
                for path in boards:
                    with kept.reading(path) as conn:
                        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        except BaseException as exc:  # surfaced below
            errors.append(exc)
        finished.wait(10)  # stay alive, like an executor thread, until every tick is done

    threads = [threading.Thread(target=tick) for _ in range(12)]
    for thread in threads:
        thread.start()
    finished.wait(10)
    try:
        assert not errors
        counts = _per_board()
        assert max(counts.values()) <= MAX_PER_BOARD, counts
        assert sum(counts.values()) <= 4 * MAX_PER_BOARD, counts
    finally:
        for thread in threads:
            thread.join(10)


def test_connection_kept_by_an_exited_thread_serves_the_next_thread(home):
    """b. A kept connection belongs to the board, not to the thread that opened it."""
    (path,) = _boards(home, 1)
    got: dict = {}

    def other():
        with kept.reading(path) as conn:
            got["conn"] = conn

    worker = threading.Thread(target=other)
    worker.start()
    worker.join(10)
    assert len(_live()) == 1
    reuses = kept.stats["reuses"]
    with kept.reading(path) as conn:
        assert conn is got["conn"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert kept.stats["reuses"] == reuses + 1
    assert len(_live()) == 1


def test_every_kept_entry_borrowed_gives_a_per_call_connection(home):
    """c. Never wait for a kept connection: past MAX_PER_BOARD concurrent borrows, a plain one closed on exit."""
    (path,) = _boards(home, 1)
    with contextlib.ExitStack() as stack:
        held = [stack.enter_context(kept.reading(path)) for _ in range(MAX_PER_BOARD)]
        assert len({id(c) for c in held}) == MAX_PER_BOARD
        assert _per_board() == {gen._resolved(str(path)): MAX_PER_BOARD}
        fallbacks = kept.stats["fallbacks"]
        with kept.reading(path) as extra:
            assert all(extra is not c for c in held)
            assert extra.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert kept.stats["fallbacks"] == fallbacks + 1
        assert _is_closed(extra)
        assert not any(_is_closed(c) for c in held)  # nothing closed under its borrower
        assert all(c.execute("SELECT 1").fetchone()[0] == 1 for c in held)
    assert len(_live()) == MAX_PER_BOARD
    with kept.reading(path) as again:
        assert any(again is c for c in held)


def test_max_kept_closes_the_least_recently_borrowed(home):
    """d. MAX_KEPT + 1 boards read in turn leave MAX_KEPT kept; the closed one is the least recently borrowed."""
    boards = _boards(home, MAX_KEPT + 1)
    conns = {}
    for path in boards[:MAX_KEPT]:
        with kept.reading(path) as conn:
            conns[path] = conn
    with kept.reading(boards[0]) as conn:  # boards[0] is now more recent than boards[1]
        assert conn is conns[boards[0]]
    with kept.reading(boards[-1]) as conn:
        conns[boards[-1]] = conn
    assert len(_live()) == MAX_KEPT
    assert _is_closed(conns[boards[1]])
    assert gen._resolved(str(boards[1])) not in _per_board()
    assert not any(_is_closed(conns[p]) for p in boards if p != boards[1])


def test_evict_retires_idle_entries_of_any_thread_and_refuses_while_borrowed(home):
    """e. Generation recovery retires every idle pool entry of the board, whichever thread borrowed it."""
    (path,) = _boards(home, 1)
    key = gen._resolved(str(path))
    readers = MAX_PER_BOARD + 1
    inside, leave = threading.Barrier(readers + 1), threading.Event()
    got: list = []

    def reader():
        with kept.reading(path) as conn:
            got.append(conn)
            inside.wait(10)  # every reader borrows at once
            leave.wait(10)

    threads = [threading.Thread(target=reader) for _ in range(readers)]
    for thread in threads:
        thread.start()
    inside.wait(10)
    leave.set()
    for thread in threads:
        thread.join(10)
    assert len(got) == readers
    assert _per_board() == {key: MAX_PER_BOARD}  # the extra reader got a per-call connection
    idle = [e.conn for e in _live()]

    kept._evict(key)
    assert all(_is_closed(c) for c in idle)
    assert _per_board() == {}

    with kept.reading(path) as conn:
        with pytest.raises(gen.DeletedWalGenerationError):
            kept._evict(key)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert os.path.exists(path)
