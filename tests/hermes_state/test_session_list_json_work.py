"""Idle sidebar polls must not parse every childless row twice."""
import sqlite3

import pytest

from hermes_state import SessionDB


def test_recent_chain_seed_does_not_parse_childless_rows(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        # No index-dependent shortcut: also cover a legacy read-only database.
        assert db._conn is not None
        db._conn.execute('DROP INDEX IF EXISTS idx_sessions_list_markers')
        db._conn.executemany(
            "INSERT INTO sessions(id,source,started_at,model_config) VALUES(?, 'cli', ?, '{}')",
            [(f'root-{i}', i + 1) for i in range(500)])
        calls = []
        with sqlite3.connect(':memory:') as native:
            def valid(value):
                calls.append(value)
                return native.execute('SELECT json_valid(?)', (value,)).fetchone()[0]
            with db._read_ctx() as conn:
                conn.create_function('json_valid', 1, valid, deterministic=True)
            db.list_sessions_rich(limit=500, compact_rows=True)
            outer_calls = len(calls)
            assert outer_calls > 0
            calls.clear()
            rows = db.list_sessions_rich(limit=10, order_by_last_active=True, compact_rows=True)
        assert [row['id'] for row in rows] == [f'root-{i}' for i in range(499, 489, -1)]
        # Compare the same filter without a chain; SQL may evaluate a marker more than once.
        assert len(calls) <= outer_calls + 50, (len(calls), outer_calls)


def test_unchanged_count_uses_stored_marker_values(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        assert db._conn is not None
        db._conn.executemany(
            "INSERT INTO sessions(id,source,started_at,model_config) VALUES(?, 'cli', ?, '{}')",
            [(f'root-{i}', i + 1) for i in range(500)])
        calls = []
        with sqlite3.connect(':memory:') as native:
            def valid(value):
                calls.append(value)
                return native.execute('SELECT json_valid(?)', (value,)).fetchone()[0]
            with db._read_ctx() as conn:
                conn.create_function('json_valid', 1, valid, deterministic=True)
            for i in range(3):
                db._conn.execute('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)', ('unrelated', str(i)))
                assert db.session_count(exclude_children=True) == 500
        assert len(calls) < 50, len(calls)


@pytest.mark.parametrize('scope', [
    {}, {'include_subagents': True}, {'include_children': True}, {'include_archived': True},
    {'archived_only': True}, {'include_pinned': True}, {'source': 'cli'}, {'sources': ['cli', 'local']},
    {'exclude_sources': ['cli']}, {'min_message_count': 1}, {'id_query': 'row'}, {'search_query': 'row'},
    {'local_created_sources': ['desktop']},
])
def test_marker_index_upgrade_preserves_legacy_listing(tmp_path, scope):
    path = tmp_path / 'state.db'
    configs = [None, '{broken', '[]', '"scalar"', '{}', '{"_delegate_from": null}',
               '{"\\u005fdelegate_from": "parent"}', '{"_branched_from": "parent"}',
               '{"_reset_from": "parent"}', b'{"_delegate_from":"parent"}']
    with SessionDB(db_path=path) as db:
        assert db._conn is not None
        db._conn.execute('DROP INDEX idx_sessions_list_markers')
        db._conn.execute("INSERT INTO sessions(id, source, started_at, end_reason) VALUES('parent','cli',1,'compression')")
        db._conn.executemany(
            "INSERT INTO sessions(id,source,created_source,started_at,model_config,parent_session_id,"
            "archived,pinned,hidden,message_count,title) VALUES(?,?,'desktop',?,?,?,?,?,?,?,?)",
            [(f'row-{i}', 'cli' if i % 2 else 'local', i + 2, config, 'parent' if i % 2 else None,
              int(i == 4), int(i == 4), int(i == 3), i, f'row {i}') for i, config in enumerate(configs)])
    def listing(db):
        return db.list_sessions_rich(limit=100, order_by_last_active=True, compact_rows=True, **scope)
    # Read-only legacy databases remain usable and do not migrate themselves.
    with SessionDB(db_path=path, read_only=True) as db:
        before = listing(db)
        count = db.session_count(exclude_children=True)
        assert db._conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_sessions_list_markers'").fetchone() is None
    with SessionDB(db_path=path) as db:
        assert db._conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_sessions_list_markers'").fetchone()
        assert listing(db) == before
        assert db.session_count(exclude_children=True) == count
        # Transactional expression values must follow writes and roll back with their rows.
        db._conn.execute('BEGIN')
        db._conn.execute("UPDATE sessions SET model_config=? WHERE id='parent'", ('{"_delegate_from":"parent"}',))
        from hermes_state_sessions import _session_filter_where
        clauses, params = _session_filter_where(exclude_children=True)
        assert db._conn.execute('SELECT count(*) FROM sessions s WHERE ' + ' AND '.join(clauses), params).fetchone()[0] == count - 1
        db._conn.rollback()
        assert db.session_count(exclude_children=True) == count
        assert listing(db) == before
        db._conn.execute("UPDATE sessions SET model_config=? WHERE id='parent'", ('{"_delegate_from":"parent"}',))
        assert db.session_count(exclude_children=True) == count - 1
