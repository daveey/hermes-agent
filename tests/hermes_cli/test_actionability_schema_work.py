"""Fresh actionability schema reads must not hand off once per column/table."""
from contextlib import closing
import sqlite3

import pytest

from hermes_cli.kanban_actionability_sources import Reader


class CountCursor(sqlite3.Cursor):
    def __iter__(self):
        while (row := super().fetchone()) is not None:
            self.connection.rows_read += 1
            yield row

    def fetchone(self):
        row = super().fetchone()
        self.connection.rows_read += row is not None
        return row


class CountConnection(sqlite3.Connection):
    rows_read = 0

    def execute(self, sql, parameters=()):
        return self.cursor(CountCursor).execute(sql, parameters)


@pytest.fixture
def conn():
    connection = sqlite3.connect(':memory:', factory=CountConnection)
    connection.execute('CREATE TABLE task_events(id,task_id,kind,payload,created_at)')
    connection.execute('CREATE TABLE task_runs(id,task_id,status,ended_at)')
    connection.execute('CREATE TABLE task_wake_checks(task_id,cmd)')
    connection.execute('CREATE TABLE kanban_failure_commands(task_id,run_uid,request_uid,operation_kind,delivery_state,canonical_receipt_json)')
    connection.execute('CREATE TABLE kanban_sync_state(mode)')
    yield connection
    connection.close()


def test_schema_result_rows_do_not_scale_with_unrelated_inventory(conn):
    baseline = Reader(conn)
    before = conn.rows_read
    Reader(conn)
    small = conn.rows_read - before
    for i in range(80):
        conn.execute(f'CREATE TABLE unrelated_{i}(a,b,c)')
    conn.execute('ALTER TABLE task_events ADD COLUMN unused_a')
    conn.execute('ALTER TABLE task_events ADD COLUMN unused_b')
    before = conn.rows_read
    current = Reader(conn)
    large = conn.rows_read - before
    assert current.event_columns == baseline.event_columns | {'unused_a', 'unused_b'}
    assert not current.partial
    assert large <= small
    assert large <= 8


def test_new_reader_sees_schema_change_without_notification(conn):
    first = Reader(conn)
    conn.execute('ALTER TABLE task_events ADD COLUMN deleted_at')
    second = Reader(conn)
    assert 'deleted_at' not in first.event_columns
    assert 'deleted_at' in second.event_columns
    conn.execute('DROP TABLE task_wake_checks')
    assert Reader(conn).partial


@pytest.mark.parametrize('factory', [bytes, lambda value: value.decode().upper()])
def test_custom_text_factory_retains_literal_names(conn, factory):
    conn.text_factory = factory
    expected_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected_columns = {row[0] for row in conn.execute('SELECT name FROM pragma_table_info(?)', ('task_events',))}
    reader = Reader(conn)
    assert reader.tables == expected_tables
    assert reader.event_columns == expected_columns


def test_pinned_snapshot_and_next_read_without_notification(tmp_path):
    path = tmp_path / 'board.db'
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE task_events(id,task_id,kind,payload,created_at)')
        with closing(sqlite3.connect(path)) as pinned:
            pinned.execute('BEGIN')
            assert 'deleted_at' not in Reader(pinned).event_columns
            writer.execute('ALTER TABLE task_events ADD COLUMN deleted_at')
            writer.commit()
            assert 'deleted_at' not in Reader(pinned).event_columns
            pinned.rollback()
            assert 'deleted_at' in Reader(pinned).event_columns


def test_same_path_replacement_during_pinned_read(tmp_path):
    path, other = tmp_path / 'board.db', tmp_path / 'replacement.db'
    for target, column in ((path, 'old_field'), (other, 'new_field')):
        connection = sqlite3.connect(target)
        try:
            connection.execute(f'CREATE TABLE task_events(id,task_id,kind,payload,created_at,{column})')
            connection.commit()
        finally:
            connection.close()
    pinned = sqlite3.connect(path)
    try:
        pinned.execute('BEGIN')
        assert 'old_field' in Reader(pinned).event_columns
        other.replace(path)
        assert 'old_field' in Reader(pinned).event_columns
        fresh = sqlite3.connect(path)
        try:
            reader = Reader(fresh)
            assert 'new_field' in reader.event_columns
            assert 'old_field' not in reader.event_columns
        finally:
            fresh.close()
    finally:
        pinned.close()


def test_minimal_connection_wrapper_retains_literal_path(conn):
    class Wrapper:
        def execute(self, *args):
            return conn.execute(*args)
    expected = Reader(conn)
    actual = Reader(Wrapper())
    assert actual.tables == expected.tables
    assert actual.schema == expected.schema
    assert actual.partial == expected.partial


def test_schema_error_still_propagates(conn):
    def deny_schema(action, first, second, database, caller):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and first == 'sqlite_master' else sqlite3.SQLITE_OK
    conn.set_authorizer(deny_schema)
    with pytest.raises(sqlite3.DatabaseError):
        Reader(conn)
