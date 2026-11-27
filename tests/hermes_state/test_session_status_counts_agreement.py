"""Controls-change publication while the sidebar counts recompute is held (t_01a21c00a2e628c8). [daveey]

A withdrawn ask must leave the canonical envelope promptly even when an unrelated count recompute holds
``_counts_flight`` and a projection holds ``_links_lock``: the empty card list's count agreement may not park
the (single, per-process) controls refresh worker. Unknown coverage stays fail-closed, never done, and the chat
refreshes again by itself once the recompute settles. Everything here goes through the real ``refresh_outcome``
entry point, real locks and a real board; nothing mocks ``_card_items``.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB

BOUND_S = 3.0  # publication bound while the locks stay held (the agreement waits at most ~1 s)


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, session_id TEXT,"
                     " block_kind TEXT, human_request TEXT)")
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [path])
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    yield path
    ksa._reset_cache_for_tests()


@pytest.fixture
def state_db(tmp_path, board):
    so._reset_hooks_for_tests()
    path = tmp_path / "state.db"
    with SessionDB(db_path=path) as handle:
        handle.create_session("chat", source="desktop")
        handle.append_message("chat", role="user", content="please fix the flaky test")
        handle.append_message("chat", role="assistant", content="ok")
    yield path
    so._reset_hooks_for_tests()


class Controls:
    """A user-steps-shaped controls provider: an open ask until withdrawn and resolved in the ledger."""

    def __init__(self):
        self.open = True

    def __call__(self, sid):
        if self.open:
            return {"decision_items": [{"id": "approval", "question": "Deploy?"}]}
        return {}


def controls_refresh(path, trigger="notice"):
    """What ``ControlsRefresher._run`` does: its own SessionDB, the quiet refresh."""
    with SessionDB(db_path=path) as handle:
        return so.refresh_outcomes_quietly(["chat"], trigger, db=handle)


def stored_status(path):
    with SessionDB(db_path=path, read_only=True) as handle:
        return (so.get_outcome("chat", db=handle) or {}).get("status") or {}


def unrelated_write(path):
    """Another chat's card: the fingerprint moves, so the published counts are no longer fresh."""
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tasks VALUES (?, 'other', 'done', 'other-chat', NULL, NULL)",
                     (f"t_{time.monotonic_ns()}",))


class Held:
    """Real ``_counts_flight`` (an unrelated recompute) and ``_links_lock`` (a projection), held by other threads."""

    def __init__(self):
        self.release = threading.Event()
        self.threads = []

    def __enter__(self):
        for lock in (ksa._counts_flight, ksa._links_lock):
            ready = threading.Event()

            def hold(lock=lock, ready=ready):
                with lock:
                    ready.set()
                    self.release.wait(30)

            thread = threading.Thread(target=hold, daemon=True)
            thread.start()
            assert ready.wait(5)
            self.threads.append(thread)
        return self

    def __exit__(self, *exc):
        self.release.set()
        for thread in self.threads:
            thread.join(10)


def wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


def is_ask(status):
    return status.get("kind") == "decision" or (status.get("presentation") or {}).get("stripe") == "stuck"


def is_done(status):
    return (status.get("kind"), (status.get("presentation") or {}).get("stripe"),
            (status.get("coverage") or {}).get("state")) == ("done", "done", "complete")


def withdrawn(path):
    status = stored_status(path)
    return None if is_ask(status) else status


def test_withdrawn_ask_leaves_the_envelope_while_counts_and_links_are_held(state_db, board):
    controls = Controls()
    so.register_outcome_provider("user-steps", controls)
    assert controls_refresh(state_db) == 1
    assert is_ask(stored_status(state_db))  # warm: the ask is published, the projection exists
    unrelated_write(board)
    with Held():
        controls.open = False  # withdrawal plus the final ledger resolve
        worker = threading.Thread(target=controls_refresh, args=(state_db,), daemon=True)
        started = time.monotonic()
        worker.start()
        status = wait_for(lambda: withdrawn(state_db), BOUND_S)
        elapsed = time.monotonic() - started
        assert status, f"withdrawn ask still published after {elapsed:.2f}s with the count flight held"
        assert not is_done(status), "unknown count agreement must never publish done"
        assert status["coverage"]["state"] == "unknown"
        worker.join(BOUND_S)
        assert not worker.is_alive(), "the controls worker stayed parked on the held count flight"
    # No further trigger: the settled recompute re-runs this chat's refresh.
    assert wait_for(lambda: is_done(stored_status(state_db)), 10), stored_status(state_db)


def test_retained_open_work_stays_unknown_after_the_deferred_refresh(state_db, board, monkeypatch):
    """Negative control: an empty list that the settled counts contradict is never done."""
    with sqlite3.connect(board) as conn:
        conn.execute("INSERT INTO tasks VALUES ('t_open', 'open', 'running', 'chat', NULL, NULL)")
    ksa._reset_cache_for_tests()
    controls = Controls()
    so.register_outcome_provider("user-steps", controls)
    controls_refresh(state_db)
    monkeypatch.setattr(ksa, "session_outcome_tasks", lambda *a, **k: [])  # the stale-empty display list
    deferred = threading.Event()
    real_deferred = so._deferred_refresh

    def observed(*args):
        try:
            real_deferred(*args)
        finally:
            deferred.set()

    monkeypatch.setattr(so, "_deferred_refresh", observed)
    unrelated_write(board)
    with Held():
        controls.open = False
        controls_refresh(state_db)
        held = stored_status(state_db)
        assert not is_ask(held) and not is_done(held)
    assert deferred.wait(10), "the settled recompute did not re-run the chat's refresh"
    status = stored_status(state_db)
    assert not is_done(status)
    assert status["coverage"]["state"] == "unknown"


def test_unreadable_counts_under_contention_fail_closed(state_db, board, monkeypatch):
    """Negative control: no snapshot and a failing recompute is unknown, not done, then recovers."""
    controls = Controls()
    controls.open = False
    so.register_outcome_provider("user-steps", controls)
    ksa._reset_cache_for_tests()
    calls = []

    def boom():
        calls.append(1)
        raise sqlite3.OperationalError("counts unavailable")

    real = ksa.active_task_counts
    monkeypatch.setattr(ksa, "active_task_counts", boom)
    controls_refresh(state_db)
    status = stored_status(state_db)
    assert calls and not is_done(status) and status["coverage"]["state"] == "unknown"
    monkeypatch.setattr(ksa, "active_task_counts", real)
    controls_refresh(state_db)
    assert is_done(stored_status(state_db))


def test_uncontended_agreement_publishes_done_in_the_same_refresh(state_db, board):
    """Control: without contention the bounded wait sees the recompute and the first refresh is final."""
    controls = Controls()
    so.register_outcome_provider("user-steps", controls)
    controls_refresh(state_db)
    unrelated_write(board)
    controls.open = False
    controls_refresh(state_db)
    assert is_done(stored_status(state_db))


class InlineThread:
    """The inline ``threading.Thread`` double several gateway suites install."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


def test_inline_thread_double_settles_without_deadlock(state_db, board, monkeypatch):
    """The recompute starts outside the flight lock: an inline start runs its settle path re-entrantly."""
    controls = Controls()
    so.register_outcome_provider("user-steps", controls)
    controls_refresh(state_db)
    unrelated_write(board)
    controls.open = False
    monkeypatch.setattr(threading, "Thread", InlineThread)
    controls_refresh(state_db)
    assert is_done(stored_status(state_db))


def test_failed_thread_start_settles_the_flight(state_db, board, monkeypatch):
    """A recompute that cannot start is a read failure (unknown), and the next refresh starts a new one."""
    from hermes_cli import kanban_counts_agreement as agreement

    controls = Controls()
    controls.open = False
    so.register_outcome_provider("user-steps", controls)

    class NoThread(InlineThread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, "Thread", NoThread)
    controls_refresh(state_db)
    assert stored_status(state_db)["coverage"]["state"] == "unknown"
    assert agreement._flights == {}
    monkeypatch.setattr(threading, "Thread", InlineThread)
    controls_refresh(state_db)
    assert is_done(stored_status(state_db))
