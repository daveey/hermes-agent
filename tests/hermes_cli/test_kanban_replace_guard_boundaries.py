"""SQL grammar and transaction-boundary controls for the Kanban factory guard."""
import sqlite3

import pytest

from hermes_cli import kanban_lockguard as factory


@pytest.mark.parametrize('sql', [
    '/* lead */ iNsErT /* x */ OR REPLACE INTO "main"."tasks" VALUES(1)',
    "REPLACE INTO 'task_runs' VALUES(1)",
    'REPLACE INTO [tasks] VALUES(1)',
    'REPLACE INTO `task_runs` VALUES(1)',
    'WITH x AS (SELECT 1) INSERT OR REPLACE INTO tasks SELECT * FROM x',
    'SELECT 1; -- next\n REPLACE INTO main.task_runs VALUES(1)',
    'UPDATE OR REPLACE tasks SET id=2 WHERE id=1',
    'WITH x AS (SELECT 1) UPDATE OR REPLACE "main"."task_runs" SET id=2',
])
def test_sqlite_spellings_refused(tmp_path, sql):
    conn = factory.connect(tmp_path / 'board.db')
    try:
        error = getattr(factory, 'ProtectedRowReplaceError', sqlite3.IntegrityError)
        with pytest.raises(error, match='REPLACE.*task'):
            conn.executescript(sql)
    finally:
        conn.close()


def test_script_refusal_does_not_commit_or_write_prefix(tmp_path):
    conn = factory.connect(tmp_path / 'board.db')
    try:
        conn.execute('CREATE TABLE tasks(id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO tasks VALUES(1)')
        error = getattr(factory, 'ProtectedRowReplaceError', sqlite3.IntegrityError)
        with pytest.raises(error, match='REPLACE.*tasks'):
            conn.executescript('INSERT INTO tasks VALUES(2); REPLACE INTO tasks VALUES(1)')
        assert conn.in_transaction
        assert conn.execute('SELECT id FROM tasks').fetchall() == [(1,)]
    finally:
        conn.close()


def test_bulk_refusal_does_not_consume_parameters(tmp_path):
    conn = factory.connect(tmp_path / 'board.db')
    consumed = []

    def parameters():
        consumed.append(True)
        yield (1,)

    try:
        conn.execute('CREATE TABLE tasks(id INTEGER PRIMARY KEY)')
        error = getattr(factory, 'ProtectedRowReplaceError', sqlite3.IntegrityError)
        with pytest.raises(error, match='REPLACE.*tasks'):
            conn.cursor().executemany('REPLACE INTO tasks VALUES(?)', parameters())
        assert consumed == []
    finally:
        conn.close()


def test_custom_cursor_factory_and_decoys(tmp_path):
    class CustomCursor(sqlite3.Cursor):
        pass

    class CustomConnection(sqlite3.Connection):
        pass

    conn = factory.connect(tmp_path / 'board.db', factory=CustomConnection)
    try:
        cursor = conn.cursor(factory=CustomCursor)
        assert isinstance(cursor, CustomCursor)
        assert isinstance(conn, CustomConnection)
        assert factory.guarded_factory(type(conn)) is type(conn)
        assert cursor.execute("SELECT 'REPLACE INTO tasks', 1 /* REPLACE INTO task_runs */").fetchone()[1] == 1
        cursor.execute('CREATE TABLE tasks(id INTEGER PRIMARY KEY)')
        error = getattr(factory, 'ProtectedRowReplaceError', sqlite3.IntegrityError)
        with pytest.raises(error, match='REPLACE.*tasks'):
            cursor.execute('REPLACE INTO tasks VALUES(1)')
        conn.execute('INSERT INTO tasks VALUES(1) ON CONFLICT(id) DO UPDATE SET id=excluded.id')
        conn.execute('CREATE TABLE "tasks_extra"(id INTEGER PRIMARY KEY)')
        conn.execute('REPLACE INTO tasks_extra VALUES(1)')
    finally:
        conn.close()
