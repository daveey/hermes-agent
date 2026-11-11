"""One busy board degrades only itself, never every sidebar row (kanban-359833be53). [daveey]

While another thread holds a board's lifecycle lock (an opener recovering a WAL generation, a long writer open),
the sidebar count read used to abort as a whole: ``board_memo_key`` re-opened the board the cheap probe had just
found busy, raised "database is locked: board lifecycle is busy" and ``annotate_kanban_activity`` marked every
row ``kanban_unknown``.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_generation as gen
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_session_activity_busy as busy
from hermes_cli import kanban_session_activity_revalidate as revalidate


def _make_board(path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))  # never the real hermes-hub replica
    ksa._reset_cache_for_tests()
    yield tmp_path
    ksa._reset_cache_for_tests()


@contextmanager
def _held(path: Path):
    """Hold ``path``'s lifecycle lock from another thread (the lock is reentrant for its own thread)."""
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with gen.lifecycle_access(path, timeout=5):
            acquired.set()
            release.wait(10)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert acquired.wait(5)
    try:
        yield
    finally:
        release.set()
        t.join(5)


def test_lifecycle_busy_error_carries_sqlite_busy(tmp_path):
    db = tmp_path / "kanban.db"
    _make_board(db, [])
    with _held(db):
        with pytest.raises(sqlite3.OperationalError) as info:
            with gen.lifecycle_access(db, timeout=0.05):
                pass
    assert isinstance(info.value, gen.BoardLifecycleBusy)
    assert info.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert gen.is_busy(info.value)
    assert not gen.is_busy(sqlite3.OperationalError("no such table: tasks"))


def test_board_memo_key_of_a_held_board_returns_a_never_matching_key(kanban_root):
    db = kanban_root / "kanban.db"
    _make_board(db, [("t1", "running", "s1")])
    with _held(db):
        key = revalidate.board_memo_key(db)
        again = revalidate.board_memo_key(db)
    assert key[0][:1] == ("stat",)
    assert key != again  # never reuses a memo row it could not vouch for


def test_one_held_board_keeps_every_row_known_and_reuses_its_last_counts(kanban_root, caplog):
    busy_db = kanban_root / "kanban.db"
    free_db = kanban_root / "kanban" / "boards" / "other" / "kanban.db"
    _make_board(busy_db, [("t1", "running", "s_busy")])
    _make_board(free_db, [("t2", "running", "s_free")])
    rows = [{"id": "s_busy"}, {"id": "s_free"}]
    ksa.annotate_kanban_activity(rows)  # warm: the busy board's last good counts are memoized
    assert [r.get("kanban_active") for r in rows] == [1, 1]

    with sqlite3.connect(free_db) as conn:  # the free board moves, so the counts are recomputed
        conn.execute("INSERT INTO tasks VALUES ('t3', 'running', 's_free')")
    rows = [{"id": "s_busy"}, {"id": "s_free"}]
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_session_activity"), _held(busy_db):
        ksa._cache = None  # past the counts TTL
        ksa.annotate_kanban_activity(rows)
    assert not any(r.get("kanban_unknown") for r in rows), rows
    assert rows[0].get("kanban_active") == 1  # last good counts of the held board
    assert rows[1].get("kanban_active") == 2  # the free board is read fresh
    assert ksa._cache_revalidatable is False  # reused counts never outlive the TTL on an unchanged fingerprint
    busy_lines = [r for r in caplog.records if "busy; reused last counts" in r.getMessage()]
    assert busy_lines and all(r.exc_info is None for r in busy_lines)
    assert not [r for r in caplog.records if "rows marked kanban_unknown" in r.getMessage()]


def test_session_tasks_on_a_held_board_reuse_the_last_rows_marked_stale(kanban_root):
    busy_db = kanban_root / "kanban.db"
    free_db = kanban_root / "kanban" / "boards" / "other" / "kanban.db"
    _make_board(busy_db, [("t1", "running", "s1")])
    _make_board(free_db, [("t2", "running", "s1")])
    assert {t["id"] for t in ksa.session_tasks(["s1"])} == {"t1", "t2"}
    with _held(busy_db):
        tasks = {t["id"]: t for t in ksa.session_tasks(["s1"])}
    assert set(tasks) == {"t1", "t2"}
    assert tasks["t1"].get("stale") is True
    assert not tasks["t2"].get("stale")


def test_first_read_waits_out_a_hold_longer_than_one_read_budget(kanban_root, monkeypatch):
    """A chat opened for the first time hit a board held a little past the 500 ms read budget: the route 500ed and
    the Desktop showed "Task status unavailable". The first read now outlasts such a hold."""
    db = kanban_root / "kanban.db"
    _make_board(db, [("t1", "running", "s1")])
    monkeypatch.setattr(busy, "FIRST_READ_BUSY_BUDGET_S", 5.0)
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with gen.lifecycle_access(db, timeout=5):
            acquired.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert acquired.wait(5)
    timer = threading.Timer(1.2, release.set)  # longer than one read's 0.5 s lifecycle budget
    timer.start()
    try:
        tasks = ksa.session_tasks(["s1"])
    finally:
        release.set()
        timer.cancel()
        holder.join(5)
    assert [(t["id"], t.get("stale")) for t in tasks] == [("t1", None)]


def test_held_board_with_no_previous_read_is_still_an_error_not_an_empty_list(kanban_root, monkeypatch):
    monkeypatch.setattr(busy, "FIRST_READ_BUSY_BUDGET_S", 0.6)
    db = kanban_root / "kanban.db"
    _make_board(db, [("t1", "running", "s1")])
    with _held(db):
        with pytest.raises(sqlite3.OperationalError):
            ksa.session_tasks(["s1"])
        rows = [{"id": "s1"}]
        ksa.annotate_kanban_activity(rows)
    assert rows[0].get("kanban_unknown") is True  # never "no work" for a board it has never read
