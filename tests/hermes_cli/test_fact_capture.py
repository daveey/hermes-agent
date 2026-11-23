"""Real guarded SQLite captures, not fake-store source parity. [daveey]"""
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_task_change as changes
from hermes_cli.fact_capture import CaptureBudget, CaptureRefused, capture_tasks
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    from hermes_cli import fact_capture as capture
    # Functional interleaving tests do not measure wall time or writer latency.
    monkeypatch.setattr(capture, "time", SimpleNamespace(monotonic=lambda: 0.0, time=time.time))
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / "kanban.db"
    conn = kbc.connect(path)
    try:
        yield path, conn
    finally:
        conn.close()


def rows(conn, ids):
    return tuple(dict(zip(("id", "title"), conn.execute("SELECT id,title FROM tasks WHERE id=?", (key,)).fetchone()))
                 for key in ids if conn.execute("SELECT 1 FROM tasks WHERE id=?", (key,)).fetchone())


def test_full_delta_delete_and_closed_before_return(board):
    path, writer = board
    a = kb.create_task(writer, title="a", assignee="w")
    borrowed = []

    def hydrate(conn, ids):
        borrowed.append(conn)
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        return rows(conn, ids)

    first = capture_tasks(path, None, hydrate)
    assert first.full and first.ids == (a,) and first.records[0]["title"] == "a"
    with pytest.raises(TypeError):
        first.records[0]["title"] = "mutable"
    with pytest.raises(sqlite3.ProgrammingError):
        borrowed[0].execute("SELECT 1")
    with kbc.write_txn(writer):
        writer.execute("DELETE FROM tasks WHERE id=?", (a,))
    second = capture_tasks(path, first.after, hydrate)
    assert not second.full and second.ids == (a,) and second.records == ()


def test_heartbeat_has_no_rows_or_hydration(board):
    path, writer = board
    a = kb.create_task(writer, title="a", assignee="w")
    first = capture_tasks(path, None, rows)
    with kbc.write_txn(writer):
        writer.execute("UPDATE tasks SET last_heartbeat_at=123 WHERE id=?", (a,))
    def forbidden(*args):
        pytest.fail("heartbeat hydrated content")
    second = capture_tasks(path, first.after, forbidden)
    assert not second.full and not second.ids and not second.records


def test_cursor_and_rows_share_snapshot_during_commit(board):
    path, writer = board
    a = kb.create_task(writer, title="old", assignee="w")
    def interleaved(conn, ids):
        with kbc.write_txn(writer):
            writer.execute("UPDATE tasks SET title='new' WHERE id=?", (a,))
        return rows(conn, ids)
    first = capture_tasks(path, None, interleaved)
    assert first.records[0]["title"] == "old"
    second = capture_tasks(path, first.after, rows)
    assert second.ids == (a,) and second.records[0]["title"] == "new"


def test_full_fallback_and_budget_refuse_partial_generation(board):
    path, writer = board
    kb.create_task(writer, title="a", assignee="w")
    first = capture_tasks(path, None, rows)
    with kbc.write_txn(writer):
        writer.execute("DROP TRIGGER kanban_activity_rev_tasks_update")
    fallback = capture_tasks(path, first.after, rows)
    assert fallback.full and fallback.after is None
    kb.create_task(writer, title="b", assignee="w")
    with pytest.raises(CaptureRefused, match="keys"):
        capture_tasks(path, None, rows, budget=CaptureBudget(max_keys=1))


def test_failed_hydration_closes_handle_and_keeps_cursor(board):
    path, writer = board
    kb.create_task(writer, title="a", assignee="w")
    borrowed = []
    def broken(conn, ids):
        borrowed.append(conn)
        raise ValueError("bad projection input")
    with pytest.raises(ValueError, match="bad projection"):
        capture_tasks(path, None, broken)
    with pytest.raises(sqlite3.ProgrammingError):
        borrowed[0].execute("SELECT 1")
    assert capture_tasks(path, None, rows).full


def test_generation_change_after_hydration_is_refused(board, monkeypatch):
    path, writer = board
    kb.create_task(writer, title="a", assignee="w")
    original = changes.changed_since
    calls = []
    def changed(conn, cursor, **kwargs):
        calls.append(cursor)
        return original(conn, cursor, **kwargs) if len(calls) == 1 else (changes.FULL, None)
    monkeypatch.setattr(changes, "changed_since", changed)
    with pytest.raises(CaptureRefused, match="generation"):
        capture_tasks(path, None, rows)


def test_large_deleted_delta_can_recover_with_small_full(board):
    path, writer = board
    a = kb.create_task(writer, title="a", assignee="w")
    first = capture_tasks(path, None, rows)
    for title in ("b", "c"):
        key = kb.create_task(writer, title=title, assignee="w")
        with kbc.write_txn(writer):
            writer.execute("DELETE FROM tasks WHERE id=?", (key,))
    result = capture_tasks(path, first.after, rows, budget=CaptureBudget(max_keys=1))
    assert result.full and result.ids == (a,)


def test_open_identity_race_is_refused(board, monkeypatch):
    from hermes_cli import fact_capture as capture
    path, writer = board
    kb.create_task(writer, title="a", assignee="w")
    real_identity = capture._identity
    def replaced(path):
        dev, ino, shm = real_identity(path)
        return dev, ino + 1, shm
    monkeypatch.setattr(capture, "_identity", replaced)
    with pytest.raises(CaptureRefused, match="opening"):
        capture_tasks(path, None, rows)


def test_commit_after_cursor_before_first_row_uses_same_snapshot(board, monkeypatch):
    path, writer = board
    kb.create_task(writer, title="old", assignee="w")
    original = changes.changed_since
    calls = []
    def cursor_then_commit(conn, cursor, **kwargs):
        result = original(conn, cursor, **kwargs)
        if not calls:
            calls.append(True)
            with kbc.write_txn(writer):
                writer.execute("UPDATE tasks SET title='new'")
        return result
    monkeypatch.setattr(changes, "changed_since", cursor_then_commit)
    result = capture_tasks(path, None, rows)
    assert result.records[0]["title"] == "old"


def test_elapsed_budget_refuses_and_closes(board, monkeypatch):
    from hermes_cli import fact_capture as capture
    path, writer = board
    kb.create_task(writer, title="a", assignee="w")
    borrowed = []
    clock = [0.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
    def slow(conn, ids):
        borrowed.append(conn)
        result = rows(conn, ids)
        clock[0] = 1.0
        return result
    with pytest.raises(CaptureRefused, match="time"):
        capture_tasks(path, None, slow)
    with pytest.raises(sqlite3.ProgrammingError):
        borrowed[0].execute("SELECT 1")


def test_briefly_held_tracking_registry_does_not_fail_capture(board, monkeypatch):
    """A capture waits out a momentary registry holder within its own budget.

    With timeout=0 every first capture on a busy hub failed with "tracking
    registry is busy" and only succeeded on the feed's retry ~0.1 s later."""
    import threading
    from hermes_cli import fact_capture as capture
    from hermes_cli import sqlite_safe_read

    path, writer = board
    kb.create_task(writer, title="a", assignee="w")
    monkeypatch.setattr(capture, "time", time)  # real clock: this test is about waiting
    held, release = threading.Event(), threading.Event()

    def holder():
        with sqlite_safe_read._live_lock:
            held.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(2)
    threading.Timer(0.005, release.set).start()
    try:
        result = capture_tasks(path, None, rows, budget=CaptureBudget(max_seconds=0.5))
    finally:
        release.set()
        thread.join(2)
    assert result.after is not None
