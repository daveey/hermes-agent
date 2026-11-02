"""Real SQLite/flock contention at the Kanban publication boundary."""
import contextlib
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import admission_fence as af
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_admission import claim_transaction

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn, tmp_path
    finally:
        conn.close()


def _task(conn, review):
    task = kb.create_task(conn, title="contention", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("review" if review else "ready", task))
    return task


@pytest.mark.parametrize("review", [False, True])
def test_sqlite_wait_does_not_block_admission_and_new_hold_wins(board, review):
    conn, home = board
    task = _task(conn, review)
    began = threading.Event()
    claim = kb.claim_review_task if review else kb.claim_task

    def worker():
        # Schema/bootstrap has already run; use a separate real DB connection.
        with contextlib.closing(sqlite3.connect(home / "kanban.db", timeout=5)) as other:
            other.row_factory = sqlite3.Row
            other.set_trace_callback(lambda sql: began.set() if sql == "BEGIN IMMEDIATE" else None)
            return claim(other, task)

    with ThreadPoolExecutor(max_workers=1) as pool:
        conn.execute("BEGIN IMMEDIATE")
        future = pool.submit(worker)
        try:
            assert began.wait(5)
            assert not future.done()
            # A blocked BEGIN must leave the global lock free. The updater can
            # install a hold before the writer is released; the claimant must
            # check that NEW hold, not a snapshot taken before its SQLite wait.
            with af.admission_lock(home, timeout=0.2):
                pass
            reservation = af.reserve(home)
            assert reservation is not None
            reservation.close()
            assert af.set_hold(home)["held"]
        finally:
            conn.execute("ROLLBACK")
        assert future.result(timeout=10) is None
    assert kb.get_task(conn, task).status == ("review" if review else "ready")
    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='claimed'").fetchone()[0] == 0


@pytest.mark.parametrize("review", [False, True])
def test_busy_admission_defers_without_waiting_or_leaking_writer(board, monkeypatch, review):
    conn, home = board
    task = _task(conn, review)
    real_lock = af.admission_lock
    attempts = []

    @contextlib.contextmanager
    def checked_lock(root, *, timeout=10):
        attempts.append(timeout)
        assert conn.in_transaction  # DB first, flock second.
        assert timeout == 0  # No sleep/retry while owning the DB writer.
        with real_lock(root, timeout=timeout) as locked:
            yield locked

    with real_lock(home):
        monkeypatch.setattr(af, "admission_lock", checked_lock)
        assert (kb.claim_review_task if review else kb.claim_task)(conn, task) is None
        assert not conn.in_transaction
        with contextlib.closing(sqlite3.connect(home / "kanban.db", timeout=0)) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
    assert attempts == [0]
    assert kb.get_task(conn, task).status == ("review" if review else "ready")
    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0


@pytest.mark.parametrize("fail_commit", [False, True])
def test_commit_retry_and_failure_remain_fenced(board, monkeypatch, fail_commit):
    conn, home = board
    task = _task(conn, False)
    boundaries = []
    real_boundary = kbc._execute_boundary_with_retry

    def boundary(connection, sql):
        if sql == "COMMIT":
            # Simulate a slow/retried COMMIT boundary. The outer flock must
            # remain held through the whole helper and through rollback.
            for _ in range(2):
                with pytest.raises(TimeoutError):
                    with af.admission_lock(home, timeout=0):
                        pytest.fail("commit escaped admission lock")
                boundaries.append(sql)
            if fail_commit:
                raise sqlite3.OperationalError("database is locked")
        return real_boundary(connection, sql)

    rollbacks = []

    def trace(sql):
        if sql == "ROLLBACK":
            try:
                with af.admission_lock(home, timeout=0):
                    rollbacks.append("unfenced")
            except TimeoutError:
                rollbacks.append("fenced")

    monkeypatch.setattr(kbc, "_execute_boundary_with_retry", boundary)
    conn.set_trace_callback(trace)
    if fail_commit:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            kb.claim_task(conn, task)
    else:
        assert kb.claim_task(conn, task).status == "running"
    assert boundaries == ["COMMIT", "COMMIT"]
    assert rollbacks == (["fenced"] if fail_commit else [])
    assert not conn.in_transaction
    with af.admission_lock(home, timeout=0):
        pass
    assert kb.get_task(conn, task).status == ("ready" if fail_commit else "running")
    assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == (0 if fail_commit else 1)


def test_body_error_rolls_back_before_unlock(board):
    conn, home = board
    task = _task(conn, False)
    with pytest.raises(ValueError, match="abort"):
        with claim_transaction(conn) as admitted:
            assert admitted
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task,))
            raise ValueError("abort")
    assert not conn.in_transaction
    assert kb.get_task(conn, task).status == "ready"
    with af.admission_lock(home, timeout=0):
        pass


@pytest.mark.parametrize("review", [False, True])
def test_busy_admission_lock_defers_quietly(board, caplog, review):
    """Designed contention (another claimer holds admission.lock for its commit) is a normal defer:
    INFO, no traceback. A WARNING with exc_info here filed false self-heal defects."""
    conn, home = board
    task = _task(conn, review)
    claim = kb.claim_review_task if review else kb.claim_task
    caplog.set_level(logging.DEBUG, logger="hermes_cli.kanban_admission")
    with af.admission_lock(home, timeout=1):  # held from another fd
        assert claim(conn, task) is None
    assert kb.get_task(conn, task).status == ("review" if review else "ready")
    records = [r for r in caplog.records if r.name == "hermes_cli.kanban_admission"]
    assert not [r for r in records if r.levelno >= logging.WARNING]
    assert not [r for r in records if r.exc_info]
    assert any(r.levelno == logging.INFO and "lock busy" in r.getMessage() for r in records)


def test_real_admission_failure_stays_loud(board, caplog, monkeypatch):
    conn, _home = board
    task = _task(conn, False)

    def broken_lock(root, *, timeout=10):
        raise PermissionError("admission.lock: permission denied")

    monkeypatch.setattr(af, "admission_lock", broken_lock)
    caplog.set_level(logging.DEBUG, logger="hermes_cli.kanban_admission")
    assert kb.claim_task(conn, task) is None
    assert kb.get_task(conn, task).status == "ready"
    warns = [r for r in caplog.records if r.name == "hermes_cli.kanban_admission" and r.levelno >= logging.WARNING]
    assert warns and warns[0].exc_info and isinstance(warns[0].exc_info[1], PermissionError)
