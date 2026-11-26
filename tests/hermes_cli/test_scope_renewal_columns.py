"""Scope renewal's task_comments column probe is one result row, not one per column. [daveey]

The actionability reader asks for it on every card and parent each session-panel pass (2,652 probes a hub pass,
2026-11-26), and CPython hands the GIL back on every result row.
"""
import sqlite3

from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli.kanban_actionability_evidence import Rows
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live


class CountCursor(sqlite3.Cursor):
    def __iter__(self):
        while (row := super().fetchone()) is not None:
            self.connection.rows_read += 1
            yield row

    def fetchone(self):
        row = super().fetchone()
        self.connection.rows_read += row is not None
        return row

    def fetchall(self):
        rows = super().fetchall()
        self.connection.rows_read += len(rows)
        return rows


class CountConnection(sqlite3.Connection):
    rows_read = 0

    def execute(self, sql, parameters=()):
        return self.cursor(CountCursor).execute(sql, parameters)


def _counted(conn):
    path = next(file for _, name, file in conn.execute('PRAGMA database_list') if name == 'main')
    counted = sqlite3.connect(path, factory=CountConnection)
    counted.execute('PRAGMA query_only=ON')
    return counted


def test_comment_columns_cross_in_one_row(live):
    counted = _counted(live)
    try:
        expected = {r[1] for r in live.execute('PRAGMA table_info(task_comments)')}
        assert len(expected) > 2
        counted.rows_read = 0
        assert renewal._columns(counted, 'task_comments') == expected
        assert counted.rows_read == 1
    finally:
        counted.close()


def test_comment_columns_through_the_actionability_wrapper(live):
    counted = _counted(live)
    try:
        expected = {r[1] for r in live.execute('PRAGMA table_info(task_comments)')}
        assert renewal._columns(Rows(counted), 'task_comments') == expected
        assert renewal._columns(Rows(counted), 'no_such_table') == set()
    finally:
        counted.close()
