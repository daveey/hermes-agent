"""Hold-only private UID classification; synthetic damaged-store controls."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_release_application as app
from hermes_cli import kanban_release_certificate as cert
from hermes_cli import kanban_release_public as public

TABLES = [(app.TABLE, 'effects', cert.MAX_BYTES), ('owner_release_executor_receipts', 'events', public.MAX_BYTES)]


@pytest.mark.parametrize('table,field,cap', TABLES)
def test_exact_private_uid_not_prose_other_task_or_consumed_identity(table, field, cap):
    with sqlite3.connect(':memory:') as conn:
        event = {'uid': 'release-uid', 'task_id': 'target'}
        assert not app.native_release(conn, event, {})
        assert app.native_release(conn, event, {'owner_release_intent_uid': None})
        conn.execute(f'CREATE TABLE {table}(task_id TEXT,evidence TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('other-task', json.dumps({field: [{'uid': 'release-uid'}]})))
        assert not app.native_release(conn, event, {})
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('target', json.dumps({field: [{'uid': 'other', 'payload': 'mentions release-uid'}]})))
        assert not app.native_release(conn, event, {})
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('target', json.dumps({field: [{'uid': 'release-uid'}]})))
        assert app.native_release(conn, event, {})
        assert not app.native_release(conn, {**event, 'uid': 'legacy'}, {})


@pytest.mark.parametrize('table,field,cap', TABLES)
@pytest.mark.parametrize('damage', ['missing-field', 'empty', 'nonlist', 'bad-effect', 'bad-uid', 'malformed', 'oversize', 'blob', 'duplicate', 'array', 'scalar', 'nonfinite'])
def test_damaged_same_task_private_evidence_is_not_legacy(table, field, cap, damage):
    evidence = {
        'missing-field': {}, 'empty': {field: []}, 'nonlist': {field: {}},
        'bad-effect': {field: ['not-an-effect']}, 'bad-uid': {field: [{'uid': None}]},
        'malformed': None, 'oversize': None, 'blob': None, 'duplicate': None,
        'array': [], 'scalar': 'not-an-envelope', 'nonfinite': {'number': float('nan')},
    }[damage]
    raw = json.dumps(evidence)
    if damage == 'duplicate':
        raw = f'{{"{field}":[{{"uid":"release-uid"}}],"{field}":[{{"uid":"other"}}]}}'
    if damage == 'malformed':
        raw = '{'
    if damage == 'oversize':
        raw = json.dumps({field: [{'uid': 'other'}], 'padding': 'x' * cap})
    if damage == 'blob':
        raw = b'{}'
    with sqlite3.connect(':memory:') as conn:
        conn.execute(f'CREATE TABLE {table}(task_id TEXT,evidence TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('target', raw))
        assert app.native_release(conn, {'uid': 'legacy', 'task_id': 'target'}, {})
        assert not app.native_release(conn, {'uid': 'legacy', 'task_id': 'unrelated'}, {})


@pytest.mark.parametrize('table,field,cap', TABLES)
def test_private_release_scan_count_bound_is_not_permission(table, field, cap):
    with sqlite3.connect(':memory:') as conn:
        conn.execute(f'CREATE TABLE {table}(task_id TEXT,evidence TEXT)')
        conn.executemany(f'INSERT INTO {table} VALUES (?,?)', [('target', json.dumps({field: [{'uid': 'other'}]}))] * 64)
        assert not app.native_release(conn, {'uid': 'legacy', 'task_id': 'target'}, {})
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('target', json.dumps({field: [{'uid': 'other'}]})))
        assert app.native_release(conn, {'uid': 'legacy', 'task_id': 'target'}, {})


@pytest.mark.parametrize('table,field,cap', TABLES)
def test_release_identity_list_and_executor_byte_bounds(table, field, cap):
    count = 35 if field == 'effects' else 38
    with sqlite3.connect(':memory:') as conn:
        conn.execute(f'CREATE TABLE {table}(task_id TEXT,evidence TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('target', json.dumps({field: [{'uid': 'other'}] * count})))
        event = {'uid': 'legacy', 'task_id': 'target'}
        assert not app.native_release(conn, event, {})
        conn.execute(f'UPDATE {table} SET evidence=?', (json.dumps({field: [{'uid': 'other'}] * (count + 1)}),))
        assert app.native_release(conn, event, {})
        # Healthy executor custody may exceed the HOME limit. The oversized HOME
        # control holds, whereas executor must not borrow that smaller cap.
        raw = json.dumps({field: [{'uid': 'other'}], 'padding': 'x' * (cert.MAX_BYTES + 1)})
        conn.execute(f'UPDATE {table} SET evidence=?', (raw,))
        assert app.native_release(conn, event, {}) == (field == 'effects')


@pytest.mark.parametrize('table,field,cap', TABLES)
def test_release_identity_bytes_are_capped_before_python_transfer(table, field, cap):
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            return self.cursor.fetchone()

        def fetchall(self):
            rows = self.cursor.fetchall()
            assert all(row[0] is None or len(row[0].encode()) <= cap for row in rows), 'oversize custody transferred before bound'
            return rows

    class Rows:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            return Cursor(self.conn.execute(sql, params))

    with sqlite3.connect(':memory:') as conn:
        conn.execute(f'CREATE TABLE {table}(task_id TEXT,evidence TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('target', 'x' * (cap + 1)))
        assert app.native_release(Rows(conn), {'uid': 'legacy', 'task_id': 'target'}, {})


@pytest.mark.parametrize('table,field,cap', TABLES)
def test_reopened_read_only_private_release_identity_and_public_mentions(tmp_path, table, field, cap):
    path = tmp_path / 'identity.db'
    with sqlite3.connect(path) as conn:
        conn.execute(f'CREATE TABLE {table}(task_id TEXT,evidence TEXT)')
        conn.execute(f'INSERT INTO {table} VALUES (?,?)', ('target', json.dumps({field: [{'uid': 'release-uid'}]})))
        conn.execute('CREATE TABLE task_events(task_id TEXT,kind TEXT,payload TEXT)')
        conn.execute('INSERT INTO task_events VALUES (?,?,?)', ('target', 'owner_release_result', json.dumps({'uid': 'legacy'})))
        conn.execute('CREATE TABLE owner_release_consumption_receipts(task_id TEXT,consume_uid TEXT,evidence TEXT)')
        conn.execute('INSERT INTO owner_release_consumption_receipts VALUES (?,?,?)', ('target', 'legacy', json.dumps({'events': [{'uid': 'legacy'}]})))
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        assert app.native_release(conn, {'uid': 'release-uid', 'task_id': 'target'}, {})
        # Public prose and consumption have no release-effect identity, even when
        # their arbitrary payloads mention this UID. Neither can veto legacy.
        assert not app.native_release(conn, {'uid': 'legacy', 'task_id': 'target'}, {})
        assert conn.total_changes == 0


@pytest.mark.parametrize('table,field,cap', TABLES)
def test_malformed_optional_release_schema_is_uncertainty(table, field, cap):
    with sqlite3.connect(':memory:') as conn:
        conn.execute(f'CREATE TABLE {table}(unrelated TEXT)')
        assert app.native_release(conn, {'uid': 'legacy', 'task_id': 'target'}, {})
