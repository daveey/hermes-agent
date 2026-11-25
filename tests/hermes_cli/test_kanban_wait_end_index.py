"""Opt-in successor probes keep ETA display work bounded. [daveey]"""
from contextlib import closing
import sqlite3

import pytest

from hermes_cli import kanban_wait_history_batch as batch
from tests.hermes_cli.test_kanban_wait_history_batch import seed


DDL = ("CREATE INDEX idx_events_wait_end ON task_events(task_id,id) WHERE kind IN "
       "('condition_met','continuation_woken','promoted','promoted_manual','unblocked',"
       "'claimed','completed','archived') AND typeof(kind)='text'")


def measured(conn):
    steps = [0]
    def progress():
        steps[0] += 1
        return 0
    conn.set_progress_handler(progress, 1)
    try:
        result = batch.capture(conn, 'ci')
    finally:
        conn.set_progress_handler(None, 0)
    return result, steps[0]


@pytest.mark.parametrize('reverse', [False, True])
def test_successor_budget_and_exact_legacy_parity(reverse, record_property):
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 150)
        conn.execute("CREATE INDEX idx_events_park_history ON task_events(id) "
                     "WHERE kind IN ('scheduled','continued') AND typeof(kind)='text'")
        conn.execute(f'PRAGMA reverse_unordered_selects={int(reverse)}')
        legacy, old_steps = measured(conn)
        conn.execute(DDL)
        indexed, new_steps = measured(conn)
        assert indexed == legacy
        record_property('legacy_steps', old_steps)
        record_property('indexed_steps', new_steps)
        print(f'legacy={old_steps} indexed={new_steps}')
        assert new_steps < old_steps * .65
        # Every call still reads current rows, including changes to an existing end.
        conn.execute("UPDATE task_events SET created_at=created_at+100 WHERE kind='condition_met'")
        changed = batch.capture(conn, 'ci')
        assert changed != indexed
        conn.execute('DROP INDEX idx_events_wait_end')
        assert batch.capture(conn, 'ci') == changed


@pytest.mark.parametrize('analyze', [False, True])
def test_unrelated_readers_cannot_select_opt_in_index(analyze):
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 3)
        conn.execute(DDL)
        if analyze:
            conn.execute('ANALYZE')
        for where in ("task_id=?", "task_id=? AND kind='completed'",
                      "task_id=? AND kind IN ('completed','claimed')"):
            with pytest.raises(sqlite3.OperationalError, match='no query solution'):
                conn.execute('SELECT id FROM task_events INDEXED BY idx_events_wait_end WHERE '+where,
                             ('t_0',)).fetchall()


@pytest.mark.parametrize('kind', ('condition_met', 'continuation_woken', 'promoted', 'promoted_manual',
                                 'unblocked', 'claimed', 'completed', 'archived'))
def test_every_end_kind_and_timestamp_order(kind):
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 1)
        conn.execute("UPDATE task_events SET kind=? WHERE kind='condition_met'", (kind,))
        legacy = batch.capture(conn, 'ci')
        assert legacy['acme/repo0'] == [1200, 900, 600]
        conn.execute(DDL)
        assert batch.capture(conn, 'ci') == legacy


def test_schema_install_upgrade_rebuild_and_settled_noop():
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(kb.SCHEMA_SQL)
        def installed():
            return conn.execute("SELECT sql FROM sqlite_schema WHERE name='idx_events_wait_end'").fetchone()[0]
        assert installed() == DDL
        conn.execute('DROP INDEX idx_events_wait_end')
        kbc._migrate_add_optional_columns(conn)
        assert installed() == DDL
        cookie = conn.execute('PRAGMA schema_version').fetchone()[0]
        kbc._migrate_add_optional_columns(conn)
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == cookie
        conn.execute('DROP TABLE task_events')
        table, indexes = kbc._REBUILD_SPECS['task_events']
        conn.execute(table)
        for ddl in indexes:
            conn.execute(ddl)
        assert installed() == DDL


def test_next_id_not_end_timestamp_and_blob_payload():
    with closing(sqlite3.connect(':memory:')) as conn:
        seed(conn, 1)
        conn.execute(DDL)
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) "
                     "VALUES ('h0-0','claimed','{}',1)")
        conn.execute('UPDATE task_events SET payload=CAST(payload AS BLOB)')
        assert batch.capture(conn, 'ci')['acme/repo0'] == [1200, 900, 600]
        # A malformed park still fails through JSON1, even with the new index.
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('bad','scheduled','{',1)")
        with pytest.raises(sqlite3.OperationalError):
            batch.capture(conn, 'ci')
