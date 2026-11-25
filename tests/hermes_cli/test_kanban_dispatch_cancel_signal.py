"""Pass-wide storage cancellation must be unswallowable by best-effort handlers."""
from contextlib import closing
import logging
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_storage as storage
from hermes_cli.kanban_db_dispatch import DispatchResult
from tests.hermes_cli import test_kanban_hold_failure_isolation as helpers

kanban_home = helpers.kanban_home
_log = logging.getLogger('tests.best_effort_service')


def test_cancellation_is_not_an_ordinary_exception():
    # Every `except Exception` / `except sqlite3.Error` service boundary, present
    # or future, lets it through; only kanban_dispatch_storage.run consumes it.
    assert issubclass(storage.DispatchWriteContended, BaseException)
    assert not issubclass(storage.DispatchWriteContended, Exception)


def _best_effort_write(conn):
    """A service nobody listed: broad catch, traceback log, then keep going."""
    try:
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_wake_checks(task_id, cmd, every, next_at) VALUES ('t_x', 'true', 120, 0)")
    except Exception:
        _log.warning('best-effort service failed', exc_info=True)


def test_unlisted_best_effort_handler_cannot_swallow_real_contention(kanban_home, caplog):
    with kbc.connect_closing() as conn:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        after = []

        def tick():
            _best_effort_write(conn)
            after.append('continued')
            return DispatchResult()

        with closing(sqlite3.connect(path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                result = storage.run(conn, tick, board='default')
            finally:
                blocker.rollback()
        assert result.storage_skip_reason == storage.REASON
        assert after == []
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r.getMessage() for r in warnings] == [
            'kanban dispatch skipped: reason=storage_write_contended board=default']
        assert warnings[0].exc_info is None
        assert not conn.in_transaction
        caplog.clear()
        assert storage.run(conn, tick, board='default').storage_skip_reason is None
        assert after == ['continued']
        assert conn.execute('SELECT count(*) FROM task_wake_checks').fetchone()[0] == 1
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_ordinary_sqlite_error_in_a_pass_stays_local(kanban_home, caplog):
    with kbc.connect_closing() as conn:
        after = []

        def tick():
            try:
                with kb.write_txn(conn):
                    conn.execute('INSERT INTO no_such_table VALUES (1)')
            except Exception:
                _log.warning('best-effort service failed', exc_info=True)
            after.append('continued')
            return DispatchResult()

        assert storage.run(conn, tick, board='default').storage_skip_reason is None
        assert after == ['continued']
        assert not conn.in_transaction


def test_commit_boundary_cancellation_releases_the_transaction(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        real = storage._Budget.execute

        def execute(self, sql):
            if sql == 'COMMIT':
                self.contended = True
                raise storage.DispatchWriteContended(storage.REASON)
            return real(self, sql)

        monkeypatch.setattr(storage._Budget, 'execute', execute)

        def tick():
            with kb.write_txn(conn):
                conn.execute("INSERT INTO task_wake_checks(task_id, cmd, every, next_at) VALUES ('t_x', 'true', 120, 0)")
            return DispatchResult()

        assert storage.run(conn, tick, board='default').storage_skip_reason == storage.REASON
        assert not conn.in_transaction
        monkeypatch.setattr(storage._Budget, 'execute', real)
        assert storage.run(conn, tick, board='default').storage_skip_reason is None
        assert conn.execute('SELECT count(*) FROM task_wake_checks').fetchone()[0] == 1


def test_writer_outside_a_dispatch_pass_keeps_operational_error(kanban_home):
    with kbc.connect_closing() as conn:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        conn.execute('PRAGMA busy_timeout = 0')
        with closing(sqlite3.connect(path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                try:
                    with kb.write_txn(conn):
                        raise AssertionError('acquired a held writer lock')
                except sqlite3.OperationalError as exc:
                    assert not isinstance(exc, storage.DispatchWriteContended)
            finally:
                blocker.rollback()
