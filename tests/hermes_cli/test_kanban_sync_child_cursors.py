"""Full-scan cursor optimization boundaries, independent of the partition fixtures."""
import sqlite3

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_diff_rows as projection
from tests.hermes_cli import sync_diff_predicate_helpers as helpers
from tests.hermes_cli.test_kanban_sync_diff_predicates import CapturingConnection


@pytest.mark.parametrize('table', helpers.TABLES)
@pytest.mark.parametrize('column', [0, 1])
@pytest.mark.parametrize('selected', [set(), {'absent'}])
def test_each_excluded_projected_text_is_decoded(table, column, selected):
    conn = sqlite3.connect(':memory:', factory=CapturingConnection)
    try:
        conn.row_factory = sqlite3.Row
        if table == 'task_links':
            conn.execute('CREATE TABLE task_links(parent_id,child_id)')
            names = ('parent_id', 'child_id')
        else:
            conn.execute(f'CREATE TABLE {table}(task_id,uid)')
            names = ('task_id', 'uid')
        values = ["'foreign'", "'uid'"]
        values[column] = "CAST(X'80' AS TEXT)"
        conn.execute(f'INSERT INTO {table}({names[0]},{names[1]}) VALUES ({values[0]},{values[1]})')
        conn.commit()
        for read in (helpers.original, client._child_uids):
            with pytest.raises(sqlite3.OperationalError, match='decode'):
                read(conn, table, selected)
        assert conn.row_factory is sqlite3.Row
        with pytest.raises(sqlite3.ProgrammingError, match='closed cursor'):
            conn.last_cursor.fetchone()
        assert not conn.in_transaction
    finally:
        conn.close()


def test_custom_factory_and_custom_cursor_are_not_replaced():
    calls = []
    def factory(cursor, row):
        calls.append(row)
        return row
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE task_comments(task_id,uid)')
        conn.executemany('INSERT INTO task_comments VALUES (?,?)', [('mine','uid'),('foreign','excluded')])
        conn.row_factory = factory
        assert client._child_uids(conn, 'task_comments', {'mine'}) == {('mine','uid')}
        assert calls == [('mine','uid'),('foreign','excluded')]
        assert conn.row_factory is factory
    finally:
        conn.close()

    class CustomCursor(sqlite3.Cursor):
        def __next__(self):
            assert self.row_factory is sqlite3.Row
            return super().__next__()
    class CustomConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            self.last_cursor = self.cursor(factory=CustomCursor)
            return self.last_cursor.execute(*args, **kwargs)
    conn = sqlite3.connect(':memory:', factory=CustomConnection)
    try:
        conn.execute('CREATE TABLE task_comments(task_id,uid)')
        conn.execute("INSERT INTO task_comments VALUES ('mine','uid')")
        conn.row_factory = sqlite3.Row
        assert client._child_uids(conn, 'task_comments', {'mine'}) == {('mine','uid')}
        assert conn.last_cursor.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_original_unparameterized_execute_arity_preserved():
    class OneArgConnection(sqlite3.Connection):
        def execute(self, sql):
            return super().execute(sql)
    conn = sqlite3.connect(':memory:', factory=OneArgConnection)
    try:
        conn.execute('CREATE TABLE task_comments(task_id,uid)')
        conn.execute("INSERT INTO task_comments VALUES ('mine','uid')")
        conn.row_factory = sqlite3.Row
        assert helpers.original(conn, 'task_comments', {'mine'}) == {('mine','uid')}
        assert client._child_uids(conn, 'task_comments', {'mine'}) == {('mine','uid')}
    finally:
        conn.close()


def test_original_execute_failure_not_retried():
    class RefusingConnection:
        calls = 0
        def execute(self, *args):
            self.calls += 1
            raise RuntimeError('execute boundary')
    conn = RefusingConnection()
    with pytest.raises(RuntimeError, match='^execute boundary$'):
        client._child_uids(conn, 'task_comments', set())
    assert conn.calls == 1


def test_sql_trace_and_explicit_snapshot_preserved(tmp_path):
    path = tmp_path/'snapshot.db'
    conn = sqlite3.connect(path)
    writer = sqlite3.connect(path)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE task_comments(task_id,uid)')
        conn.execute("INSERT INTO task_comments VALUES ('mine','old')")
        conn.commit()
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        original = helpers.original(conn, 'task_comments', {'mine'})
        writer.execute("UPDATE task_comments SET uid='new'")
        writer.commit()
        trace = []
        conn.set_trace_callback(trace.append)
        assert client._child_uids(conn, 'task_comments', {'mine'}) == original
        assert trace == ['SELECT task_id, uid FROM task_comments WHERE uid IS NOT NULL']
        assert conn.in_transaction
        conn.rollback()
        assert client._child_uids(conn, 'task_comments', {'mine'}) == {('mine','new')}
    finally:
        writer.close()
        conn.close()


def test_actual_tracked_kanban_connection_uses_native_cursor(tmp_path):
    from hermes_cli import kanban_db_connect as db
    from hermes_cli import sqlite_safe_read
    path = tmp_path/'tracked.db'
    conn = db._sqlite_connect(path)
    try:
        conn.execute('CREATE TABLE task_comments(task_id,uid)')
        conn.execute("INSERT INTO task_comments VALUES ('mine','uid')")
        conn.row_factory = sqlite3.Row
        with projection._child_query(conn, 'SELECT task_id,uid FROM task_comments') as rows:
            from hermes_cli.kanban_replace_guard import _cursor_factory
            assert type(rows) is _cursor_factory(sqlite3.Cursor)
            assert rows.row_factory is None
            assert list(rows) == [('mine','uid')]
        with pytest.raises(sqlite3.ProgrammingError, match='closed cursor'):
            rows.fetchone()
        assert conn.row_factory is sqlite3.Row
        assert client._child_uids(conn, 'task_comments', {'mine'}) == {('mine','uid')}
        assert not conn.in_transaction
        with pytest.raises(sqlite_safe_read.LiveConnectionError):
            with sqlite_safe_read.offline_file_access(path):
                pass
    finally:
        conn.close()
