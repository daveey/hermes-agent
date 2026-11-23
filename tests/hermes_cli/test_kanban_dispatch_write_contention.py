"""A SQLite writer must not monopolize the per-board dispatcher pass."""
from contextlib import closing
import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_owner as owner
from hermes_cli.kanban_db_dispatch import DispatchResult


@pytest.mark.parametrize("entry", ["write_txn", "direct", "maintenance", "swallowed"])
def test_contending_writer_returns_recorded_skip_before_writer_releases(tmp_path, monkeypatch, caplog, entry):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli import kanban_model_retirement, kanban_ratio_routing
    monkeypatch.setattr(kanban_model_retirement, "sweep", lambda conn: None)
    monkeypatch.setattr(kanban_ratio_routing, "begin_tick", lambda conn: None)
    path = home / "kanban.db"
    acquired, release = threading.Event(), threading.Event()
    failures = []

    def writer():
        try:
            with closing(sqlite3.connect(path)) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                acquired.set()
                release.wait(3)
                blocker.rollback()
        except sqlite3.Error as exc:
            failures.append(exc)
            acquired.set()

    with closing(kbc.connect(path)) as conn:
        conn.execute("CREATE TABLE contention_probe (value INTEGER)")
        original_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        worker = threading.Thread(target=writer)
        worker.start()
        try:
            assert acquired.wait(5)
            assert not failures

            def write():
                if entry == "direct":
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("INSERT INTO contention_probe VALUES (1)")
                    conn.commit()
                else:
                    with kb.write_txn(conn):
                        conn.execute("INSERT INTO contention_probe VALUES (1)")

            def tick():
                if entry == "swallowed":
                    try:
                        write()
                    except sqlite3.OperationalError:
                        pass
                else:
                    write()
                return DispatchResult()

            if entry == "maintenance":
                monkeypatch.setattr(kanban_model_retirement, "sweep", lambda conn: write())
                callback = lambda: DispatchResult()
            else:
                callback = tick
            started = time.monotonic()
            result = owner.run_owned_tick(conn, callback, None, "default", False)
            elapsed = time.monotonic() - started
            assert getattr(result, "storage_skip_reason", None) == "storage_write_contended"
            assert elapsed < 2
            assert "reason=storage_write_contended" in caplog.text
            assert not conn.in_transaction
            assert conn.execute("SELECT count(*) FROM contention_probe").fetchone()[0] == 0
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == original_timeout
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive()
        assert not failures
        # The dispatcher lock and SQLite connection remain usable on the next pass.
        monkeypatch.setattr(kanban_model_retirement, "sweep", lambda conn: None)
        result = owner.run_owned_tick(conn, tick, None, "default", False)
        assert not result.skipped_locked
        assert conn.execute("SELECT count(*) FROM contention_probe").fetchone()[0] == 1


def test_nonbusy_errors_propagate_and_restore_timeout():
    from hermes_cli import kanban_dispatch_storage as storage
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("PRAGMA busy_timeout = 4321")
        def work():
            kbc._execute_boundary_with_retry(conn, "NOT SQL")
        with pytest.raises(sqlite3.OperationalError, match="syntax error"):
            storage.run(conn, work, board="sample")
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
        assert storage.execute_boundary(conn, "SELECT 1") is False


def test_non_sql_work_does_not_spend_acquisition_allowance(monkeypatch):
    from hermes_cli import kanban_dispatch_storage as storage
    now = [100.0]
    monkeypatch.setattr(storage, "_clock", lambda: now[0])
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as conn:
        conn.execute("CREATE TABLE example (value INTEGER)")
        def work():
            now[0] += storage.WRITE_WAIT_SECONDS + 1
            kbc._execute_boundary_with_retry(conn, "BEGIN IMMEDIATE")
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(storage.WRITE_WAIT_SECONDS * 1000)
            conn.execute("INSERT INTO example VALUES (1)")
            kbc._execute_boundary_with_retry(conn, "COMMIT")
            return DispatchResult()
        result = storage.run(conn, work, board="sample")
        assert result.storage_skip_reason is None
        assert conn.execute("SELECT count(*) FROM example").fetchone()[0] == 1


def test_ordinary_writer_keeps_retry_policy(monkeypatch):
    calls = []
    class Connection:
        def execute(self, sql):
            calls.append(sql)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(kbc.time, "sleep", lambda seconds: None)
    kbc._execute_boundary_with_retry(Connection(), "BEGIN IMMEDIATE")
    assert calls == ["BEGIN IMMEDIATE", "BEGIN IMMEDIATE"]


def test_other_connection_is_not_dispatch_scoped():
    from hermes_cli import kanban_dispatch_storage as storage
    with closing(sqlite3.connect(":memory:")) as conn, closing(sqlite3.connect(":memory:")) as other:
        def work():
            assert storage.execute_boundary(other, "SELECT 1") is False
            return DispatchResult()
        storage.run(conn, work, board="sample")


def test_commit_contention_rolls_back_without_replaying_body(tmp_path, monkeypatch):
    from hermes_cli import kanban_dispatch_storage as storage
    monkeypatch.setattr(storage, "WRITE_WAIT_SECONDS", 0.05)
    path = tmp_path / "commit-contention.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("CREATE TABLE example (value INTEGER)")
        with closing(sqlite3.connect(path, isolation_level=None)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM example").fetchall()
            bodies = []
            def work():
                with kb.write_txn(conn):
                    bodies.append(True)
                    conn.execute("INSERT INTO example VALUES (1)")
                return DispatchResult()
            try:
                result = storage.run(conn, work, board="sample")
                assert result.storage_skip_reason == storage.REASON
                assert bodies == [True]
                assert not conn.in_transaction
                assert conn.execute("SELECT count(*) FROM example").fetchone()[0] == 0
            finally:
                reader.rollback()


def test_storage_skip_is_named_in_dispatch_suppression():
    from hermes_cli.kanban_db_dispatch_stuck import describe_suppression
    result = DispatchResult(storage_skip_reason="storage_write_contended")
    assert "storage_write_contended=1" in describe_suppression([result])


def test_swallowed_preclaim_refusal_stops_before_pickup():
    from hermes_cli import kanban_dispatch_storage as storage
    with closing(sqlite3.connect(":memory:")) as conn:
        picked_up = []
        def work():
            budget = storage._active.get()
            assert budget is not None
            budget.contended = True  # maintenance caught its boundary refusal
            storage.finish_preclaim(conn)
            picked_up.append(True)
            return DispatchResult()
        assert storage.run(conn, work, board="sample").storage_skip_reason == storage.REASON
        assert picked_up == []


def test_postclaim_error_is_not_replaced_by_an_empty_result():
    from hermes_cli import kanban_dispatch_storage as storage
    with closing(sqlite3.connect(":memory:")) as conn:
        error = sqlite3.OperationalError("database is locked")
        def work():
            storage.finish_preclaim(conn)
            assert not storage.execute_boundary(conn, "BEGIN IMMEDIATE")
            raise error
        with pytest.raises(sqlite3.OperationalError) as caught:
            storage.run(conn, work, board="sample")
        assert caught.value is error



def test_gateway_storage_skip_counts_as_skipped_pass(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from gateway import kanban_watchers_dispatcher as watcher
    skips = []
    @contextmanager
    def accounting(name):
        yield SimpleNamespace(skip=lambda: skips.append(name))
    monkeypatch.setattr(watcher._la, "pass_", accounting)
    result = DispatchResult(storage_skip_reason="storage_write_contended")
    dispatcher = SimpleNamespace(tick_once_for_board=lambda slug: result)
    assert watcher._KanbanDispatcher._board_pass(dispatcher, "sample") is result
    assert skips == ["dispatcher.board/sample"]
