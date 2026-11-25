"""Park-history capture must not sort the complete park inventory. [daveey]"""
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_wait_history_batch as batch
from tests.hermes_cli import test_kanban_wait_history_batch as helpers
from tests.hermes_cli import test_kanban_wait_eta as eta_helpers


DDL = ("CREATE INDEX idx_events_park_history ON task_events(id) "
       "WHERE kind IN ('scheduled','continued') AND typeof(kind)='text'")


@pytest.mark.parametrize('analyze', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_capture_vm_work_is_bounded_by_recent_matching_parks(analyze, reverse):
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 1)
        # Older matching parks dwarf the bounded recent capture. Each has a
        # distinct task so the old kind/task/id index cannot satisfy id order.
        conn.executemany(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'scheduled',?,1)",
            [(f'old{i}', '{"awaiting":{"kind":"ci","ref":"https://github.com/acme/old/actions/runs/1"}}')
             for i in range(20_000)])
        for i in range(650):
            eta_helpers._finished(conn, f'recent{i}', 'ci',
                'https://github.com/acme/recent/actions/runs/1', 900)
        conn.execute(DDL)
        if analyze:
            conn.execute('ANALYZE')
        conn.execute(f'PRAGMA reverse_unordered_selects={int(reverse)}')
        expected = {'acme/recent': [900.0] * 20}
        steps = [0]
        def progress():
            steps[0] += 100
            return 0
        conn.set_progress_handler(progress, 100)
        actual = batch.capture(conn, 'ci')
        conn.set_progress_handler(None, 0)
        assert actual == expected
        assert steps[0] < 150_000, steps[0]
        print(f'capture analyze={analyze} reverse={reverse}: {steps[0]} VM steps')


@pytest.mark.parametrize('blob', [False, True])
def test_indexed_capture_matches_legacy_and_next_call_mutation(blob):
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 40)
        if blob:
            conn.execute('UPDATE task_events SET payload=CAST(payload AS BLOB)')
        expected = batch.capture(conn, 'ci')
        conn.execute(DDL)
        assert batch.capture(conn, 'ci') == expected
        conn.execute("UPDATE task_events SET created_at=created_at+7 WHERE kind='condition_met'")
        changed = batch.capture(conn, 'ci')
        assert changed != expected
        conn.execute('DROP INDEX idx_events_park_history')
        assert batch.capture(conn, 'ci') == changed


def test_index_is_not_eligible_for_existing_event_queries():
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 1)
        conn.execute(DDL)
        conn.execute('ANALYZE')
        # Explicit typeof is an opt-in, not an index every existing consumer
        # can silently choose (and thereby change unspecified traversal order).
        with pytest.raises(sqlite3.OperationalError, match='no query solution'):
            conn.execute("SELECT id FROM task_events INDEXED BY idx_events_park_history "
                         "WHERE kind IN ('scheduled','continued')").fetchall()


def test_fresh_upgrade_rebuild_and_settled_schema_match():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(kb.SCHEMA_SQL)
        def index_sql():
            return conn.execute("SELECT sql FROM sqlite_schema WHERE name='idx_events_park_history'").fetchone()[0]
        fresh = index_sql()
        conn.execute('DROP INDEX idx_events_park_history')
        kbc._migrate_add_optional_columns(conn)
        assert index_sql() == fresh
        cookie = conn.execute('PRAGMA schema_version').fetchone()[0]
        kbc._migrate_add_optional_columns(conn)
        assert conn.execute('PRAGMA schema_version').fetchone()[0] == cookie
        conn.execute('DROP TABLE task_events')
        table, indexes = kbc._REBUILD_SPECS['task_events']
        conn.execute(table)
        for ddl in indexes:
            conn.execute(ddl)
        assert index_sql() == fresh


@pytest.mark.parametrize('kind', ['ci', 'deploy'])
@pytest.mark.parametrize('reverse', [False, True])
def test_indexed_cap_first_end_and_mutations_match_literal(kind, reverse):
    from hermes_cli import kanban_wait_history as history
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 1)
        for i in range(history.SCAN_CAP + 20):
            eta_helpers._finished(conn, f'x{i}', kind,
                'https://github.com/acme/widgets/actions/runs/1' if kind == 'ci' else 'hub/core#1',
                600 if i < 20 else 900, start_kind='continued' if i % 2 else 'scheduled')
        # Later end has an earlier timestamp: select first id, not MIN(time).
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('x619','claimed','{}',1)")
        conn.execute(f'PRAGMA reverse_unordered_selects={int(reverse)}')
        literal = batch.capture(conn, kind)
        conn.execute(DDL)
        assert batch.capture(conn, kind) == literal
        conn.execute("UPDATE task_events SET created_at=created_at+7 WHERE kind='condition_met'")
        changed = batch.capture(conn, kind)
        assert changed != literal
        conn.execute('DROP INDEX idx_events_park_history')
        assert batch.capture(conn, kind) == changed


def test_indexed_capture_retains_malformed_recent_payload_failure():
    with closing(sqlite3.connect(':memory:')) as conn:
        helpers.seed(conn, 1)
        conn.execute(DDL)
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('bad','scheduled','{',1)")
        with pytest.raises(sqlite3.OperationalError, match='malformed JSON'):
            batch.capture(conn, 'ci')
