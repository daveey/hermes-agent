"""A returned cached reader must not retain the previous borrow's snapshot."""
import contextlib
import sqlite3
import weakref

import pytest

from hermes_cli import kanban_session_activity as activity


@pytest.mark.parametrize('mode', ['clean', 'transaction', 'cursor', 'custom_cursor'])
@pytest.mark.parametrize('body_error', [False, True])
def test_returned_reader_releases_snapshot(tmp_path, mode, body_error):
    path = tmp_path / 'reader.db'

    class CustomCursor(sqlite3.Cursor):
        pass

    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as writer:
        writer.execute('PRAGMA journal_mode=WAL').fetchall()
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE t(v INTEGER)')
        writer.executemany('INSERT INTO t VALUES (?)', [(1,), (2,), (3,)])
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        writer.execute('INSERT INTO t VALUES (4)')
        cursor = None
        reader = None
        retained_error = None
        try:
            try:
                with activity.cached_ro(path, busy_budget=0) as reader:
                    if mode == 'transaction':
                        reader.execute('BEGIN')
                    cursor = (reader.cursor(CustomCursor).execute('SELECT v FROM t')
                              if mode == 'custom_cursor' else reader.execute('SELECT v FROM t'))
                    if mode in ('cursor', 'custom_cursor'):
                        assert cursor.fetchone() == (1,)
                        assert not reader.in_transaction  # SQLite hides implicit SELECT snapshots.
                    else:
                        assert len(cursor.fetchall()) == 4
                        cursor.close()
                        cursor = None
                    if body_error:
                        raise ValueError('retained borrower traceback')
            except ValueError as exc:
                retained_error = exc  # Keep the actual failing frame alive through the checkpoint.
            assert (retained_error is not None) == body_error
            writer.execute('INSERT INTO t VALUES (5)')
            checkpoint = writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()
            assert checkpoint[0] == 0, (mode, body_error, checkpoint)
            if cursor is not None:
                with pytest.raises(sqlite3.ProgrammingError, match='closed cursor'):
                    cursor.fetchone()
            assert reader is not None
            assert not reader.in_transaction
            assert reader.execute('PRAGMA busy_timeout').fetchone() == (500,)
            with activity.cached_ro(path) as again:
                assert again is reader  # Retain open amortization, not the previous snapshot.
                assert again.execute('SELECT count(*) FROM t').fetchone() == (5,)
        finally:
            if cursor is not None:
                cursor.close()
            if reader is not None:
                with contextlib.suppress(sqlite3.Error):
                    reader.rollback()
            activity.evict_readers(str(path.resolve()))


def test_python_cursor_close_cannot_interrupt_sqlite_snapshot_cleanup(tmp_path):
    path = tmp_path / 'reader.db'
    calls = []

    class CustomCursor(sqlite3.Cursor):
        def close(self):
            calls.append('custom close')
            raise TypeError('custom teardown error')

    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as writer:
        writer.execute('PRAGMA journal_mode=WAL').fetchall()
        writer.execute('CREATE TABLE t(v)')
        writer.executemany('INSERT INTO t VALUES (?)', [(1,), (2,), (3,)])
        cursor = None
        try:
            with activity.cached_ro(path, busy_budget=0) as reader:
                cursor = reader.cursor(CustomCursor).execute('SELECT v FROM t')
                assert cursor.fetchone() == (1,)
            writer.execute('INSERT INTO t VALUES (4)')
            assert writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0] == 0
            assert calls == []  # Finalize SQLite, not arbitrary Python callbacks under a borrower lock.
            assert reader.execute('PRAGMA busy_timeout').fetchone() == (500,)
        finally:
            if cursor is not None:
                with contextlib.suppress(sqlite3.Error):
                    sqlite3.Cursor.close(cursor)
            activity.evict_readers(str(path.resolve()))


def test_private_fallback_finalizes_cursor_before_close(tmp_path):
    path = tmp_path / 'reader.db'
    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as writer:
        writer.execute('PRAGMA journal_mode=WAL').fetchall()
        writer.execute('CREATE TABLE t(v)')
        writer.executemany('INSERT INTO t VALUES (?)', [(1,), (2,), (3,)])
        cursor = None
        try:
            with activity.cached_ro(path) as shared:
                with activity.cached_ro(path, busy_budget=0) as private:
                    assert private is not shared
                    cursor = private.execute('SELECT v FROM t')
                    assert cursor.fetchone() == (1,)
                writer.execute('INSERT INTO t VALUES (4)')
                assert writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0] == 0
        finally:
            if cursor is not None:
                with contextlib.suppress(sqlite3.Error):
                    sqlite3.Cursor.close(cursor)
            activity.evict_readers(str(path.resolve()))


def test_sqlite_body_error_finalizes_cursor_before_connection_retirement(tmp_path):
    path = tmp_path / 'reader.db'
    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as writer:
        writer.execute('PRAGMA journal_mode=WAL').fetchall()
        writer.execute('CREATE TABLE t(v)')
        writer.executemany('INSERT INTO t VALUES (?)', [(1,), (2,), (3,)])
        cursor = None
        try:
            with pytest.raises(sqlite3.OperationalError, match='missing_table'):
                with activity.cached_ro(path) as reader:
                    cursor = reader.execute('SELECT v FROM t')
                    assert cursor.fetchone() == (1,)
                    reader.execute('SELECT * FROM missing_table')
            writer.execute('INSERT INTO t VALUES (4)')
            assert writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0] == 0
            with activity.cached_ro(path) as again:
                assert again is not reader
                assert again.execute('SELECT count(*) FROM t').fetchone() == (4,)
        finally:
            if cursor is not None:
                with contextlib.suppress(sqlite3.Error):
                    sqlite3.Cursor.close(cursor)
            activity.evict_readers(str(path.resolve()))


def test_cleanup_failure_evicts_reader_without_masking_body(tmp_path, monkeypatch):
    path = tmp_path / 'reader.db'
    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as writer:
        writer.execute('PRAGMA journal_mode=WAL').fetchall()
        writer.execute('CREATE TABLE t(v)')
        writer.executemany('INSERT INTO t VALUES (?)', [(1,), (2,), (3,)])
        try:
            with pytest.raises(ValueError, match='body failure'):
                with activity.cached_ro(path, busy_budget=0) as reader:
                    reader.execute('BEGIN')
                    cursor = reader.execute('SELECT v FROM t')
                    assert cursor.fetchone() == (1,)

                    def fail_cleanup():
                        raise sqlite3.ProgrammingError('injected rollback failure')

                    monkeypatch.setattr(reader, 'rollback', fail_cleanup)
                    raise ValueError('body failure')
            writer.execute('INSERT INTO t VALUES (4)')
            assert writer.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0] == 0
            with activity.cached_ro(path) as again:
                assert again is not reader
                assert again.execute('SELECT count(*) FROM t').fetchone() == (4,)
        finally:
            activity.evict_readers(str(path.resolve()))


def test_cursor_tracking_does_not_keep_discarded_results_alive(tmp_path):
    path = tmp_path / 'reader.db'
    with contextlib.closing(sqlite3.connect(path)) as writer:
        writer.execute('CREATE TABLE t(v)')
    reader = None
    try:
        with activity.cached_ro(path) as reader:
            cursor = reader.execute('SELECT 1')
            ref = weakref.ref(cursor)
            del cursor
            assert ref() is None
        with activity.cached_ro(path) as again:
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                again.execute('INSERT INTO t VALUES (1)')
    finally:
        if reader is not None:
            with contextlib.suppress(sqlite3.Error):
                reader.rollback()
        activity.evict_readers(str(path.resolve()))
