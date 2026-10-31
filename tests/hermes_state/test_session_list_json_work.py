"""Idle sidebar polls must not parse every childless row twice."""
import sqlite3

import pytest

from hermes_state import SessionDB


def test_recent_chain_seed_does_not_parse_childless_rows(tmp_path):
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
            db.list_sessions_rich(limit=500, compact_rows=True)
            outer_calls = len(calls)
            assert outer_calls > 0
            calls.clear()
            rows = db.list_sessions_rich(limit=10, order_by_last_active=True, compact_rows=True)
        assert [row['id'] for row in rows] == [f'root-{i}' for i in range(499, 489, -1)]
        # Compare the same filter without a chain; SQL may evaluate a marker more than once.
        assert len(calls) <= outer_calls + 50, (len(calls), outer_calls)


@pytest.mark.parametrize('scope', [
    {}, {'include_subagents': True}, {'include_children': True}, {'include_archived': True},
    {'archived_only': True}, {'include_pinned': True}, {'source': 'cli'}, {'sources': ['cli', 'local']},
    {'exclude_sources': ['cli']}, {'min_message_count': 1}, {'id_query': 'row'}, {'search_query': 'row'},
    {'local_created_sources': ['desktop']},
])
def test_parent_seed_preserves_listing(tmp_path, scope, monkeypatch):
    path = tmp_path / 'state.db'
    configs = [None, '{broken', '[]', '"scalar"', '{}', '{"_delegate_from": null}',
               '{"\\u005fdelegate_from": "parent"}', '{"_branched_from": "parent"}',
               '{"_reset_from": "parent"}', b'{"_delegate_from":"parent"}']
    with SessionDB(db_path=path) as db:
        assert db._conn is not None
        db._conn.execute("INSERT INTO sessions(id, source, started_at, end_reason) VALUES('parent','cli',1,'compression')")
        db._conn.executemany(
            "INSERT INTO sessions(id,source,created_source,started_at,model_config,parent_session_id,"
            "archived,pinned,hidden,message_count,title) VALUES(?,?,'desktop',?,?,?,?,?,?,?,?)",
            [(f'row-{i}', 'cli' if i % 2 else 'local', i + 2, config, 'parent' if i % 2 else None,
              int(i == 4), int(i == 4), int(i == 3), i, f'row {i}') for i, config in enumerate(configs)])
    with SessionDB(db_path=path, read_only=True) as db:
        actual = db.list_sessions_rich(limit=100, order_by_last_active=True, compact_rows=True, **scope)
        read_all = db._read_all
        def old_seed(sql, params=()):
            # Differential oracle: execute the former correlated seed on the same fixture/snapshot.
            driven = ('(SELECT DISTINCT parent_session_id FROM sessions WHERE parent_session_id IS NOT NULL) roots '
                      'CROSS JOIN sessions s ON s.id = roots.parent_session_id')
            if 'WITH RECURSIVE chain(root_id, cur_id)' in sql and not (scope.get('id_query') or scope.get('search_query')):
                assert driven in sql
            if driven in sql:
                sql = sql.replace(driven, 'sessions s', 1).replace(
                    '                    UNION ALL',
                    ' AND EXISTS (SELECT 1 FROM sessions child_seed WHERE child_seed.parent_session_id = s.id)\n'
                    '                    UNION ALL', 1)
            return read_all(sql, params)
        monkeypatch.setattr(db, '_read_all', old_seed)
        assert db.list_sessions_rich(limit=100, order_by_last_active=True, compact_rows=True, **scope) == actual
