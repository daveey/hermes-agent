"""Factory-level REPLACE protection for PR4045; no SQLite trigger dependency."""
import sqlite3

import pytest

from hermes_cli import kanban_lockguard as factory


@pytest.mark.parametrize('table', ['tasks', 'task_runs'])
@pytest.mark.parametrize('verb', ['REPLACE', 'INSERT OR REPLACE'])
@pytest.mark.parametrize('surface', ['execute', 'executemany', 'executescript', 'cursor', 'returned_cursor'])
def test_factory_refuses_protected_replacement(tmp_path, table, verb, surface):
    conn = factory.connect(tmp_path / 'board.db')
    try:
        conn.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY, value TEXT)')
        conn.execute(f"INSERT INTO {table} VALUES ('held', 'original')")
        conn.commit()
        conn.execute('PRAGMA recursive_triggers=OFF')
        sql = f"{verb} INTO {table} VALUES ('held', 'overwritten')"
        error = getattr(factory, 'ProtectedRowReplaceError', sqlite3.IntegrityError)
        with pytest.raises(error, match='REPLACE.*tasks|REPLACE.*task_runs'):
            if surface == 'executemany':
                conn.executemany(sql, [()])
            elif surface == 'cursor':
                conn.cursor().execute(sql)
            elif surface == 'returned_cursor':
                conn.execute('SELECT 1').execute(sql)
            else:
                getattr(conn, surface)(sql)
        assert conn.execute(f'SELECT value FROM {table}').fetchone()[0] == 'original'
    finally:
        conn.close()


def test_other_table_replace_and_upsert_unchanged(tmp_path):
    conn = factory.connect(tmp_path / 'board.db')
    try:
        conn.execute('CREATE TABLE other(id INTEGER PRIMARY KEY, value TEXT)')
        conn.execute("INSERT INTO other VALUES(1, 'a')")
        conn.execute("INSERT OR REPLACE INTO other VALUES(1, 'b')")
        conn.execute("INSERT INTO other VALUES(1, 'c') ON CONFLICT(id) DO UPDATE SET value=excluded.value")
        assert conn.execute('SELECT value FROM other').fetchone()[0] == 'c'
    finally:
        conn.close()
