"""Disposable creator certificates never survive the real rebuild/fill path."""
import sqlite3

import pytest

from hermes_cli import kanban_creator_empty as empty
from hermes_cli import kanban_db_recover as recovery


def database(path, *, markers=True):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY, session_id TEXT);"
                       "CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);"
                       "CREATE INDEX events_task ON task_events(task_id);"
                       "INSERT INTO tasks VALUES ('child',NULL);"
                       "INSERT INTO task_events VALUES (7,'child','created','{\"creator_task_id\":\"late\"}');")
    if markers:
        assert empty.ensure(conn)
        # A stale certificate, as can remain after incomplete salvage / backup fill.
        conn.execute('INSERT INTO kanban_creator_empty_events VALUES (7)')
    return conn


@pytest.mark.parametrize('markers', [False, True])
def test_real_rebuild_preserves_schema_but_discards_certificates(tmp_path, markers):
    source, out = tmp_path/'source.db', tmp_path/'out.db'
    conn = database(source, markers=markers)
    schema = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
    conn.close()
    result = recovery.RecoveryResult('failed', str(source))
    recovery._build(source, out, None, result)
    with sqlite3.connect(out, isolation_level=None) as rebuilt:
        # Never-enrolled rebuilds keep the original schema; they must not invent recovery_loss.
        assert rebuilt.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert rebuilt.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_replay_recovery_loss1'"
        ).fetchone() is None
        if markers:
            assert rebuilt.execute('SELECT * FROM kanban_creator_empty_events').fetchall() == []
            assert result.tables[empty._TABLE].recovered == 0
        rebuilt.row_factory = sqlite3.Row
        assert [r['id'] for r in empty.candidates(rebuilt)] == ['child']


def test_real_rebuild_fills_event_but_not_stale_backup_certificate(tmp_path, monkeypatch):
    source, backup, out = (tmp_path/name for name in ('source.db','backup.db','out.db'))
    database(source).close()
    database(backup).close()
    salvage = recovery._salvage_table

    def damaged(src, dst, table, sql):
        if table == 'task_events':
            # Index-readable row, unavailable table page; real backup fill must restore it.
            return 0, ['id','task_id','kind','payload'], 1, {'events_task': (['task_id'], {7: ('child',)})}, set()
        return salvage(src, dst, table, sql)

    monkeypatch.setattr(recovery, '_salvage_table', damaged)
    result = recovery.RecoveryResult('failed', str(source))
    recovery._build(source, out, backup, result)
    with sqlite3.connect(out, isolation_level=None) as rebuilt:
        assert rebuilt.execute('SELECT payload FROM task_events').fetchone()[0] == '{"creator_task_id":"late"}'
        assert result.tables['task_events'].filled_from_backup == 1
        assert rebuilt.execute('SELECT * FROM kanban_creator_empty_events').fetchall() == []
        assert empty.ensure(rebuilt)
        rebuilt.row_factory = sqlite3.Row
        assert [r['id'] for r in empty.candidates(rebuilt)] == ['child']


def test_backup_fill_itself_refuses_disposable_marker_data(tmp_path):
    backup = tmp_path/'backup.db'
    database(backup).close()
    with sqlite3.connect(':memory:', isolation_level=None) as dst:
        dst.execute(empty._TABLE_SQL)
        result = recovery.RecoveryResult('failed', ':memory:')
        recovery._fill_from_backup(dst, backup, [(empty._TABLE, empty._TABLE_SQL)],
                                   {empty._TABLE: {'marker_index': (['event_id'], {7: (7,)})}}, result)
        assert dst.execute('SELECT * FROM kanban_creator_empty_events').fetchall() == []
