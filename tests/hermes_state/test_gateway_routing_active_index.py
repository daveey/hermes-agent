"""Sparse routing reads must retain the literal JSON/error and row-order semantics."""
import json
import sqlite3
from contextlib import closing

import pytest

from hermes_state import SessionDB
from hermes_state_routing_snapshot import _scan

INDEX = 'idx_gateway_routing_active_snapshot'
LITERAL = ("SELECT scope, entry_json, json_extract(entry_json, '$.session_id') AS sid"
           " FROM gateway_routing WHERE json_extract(entry_json, '$.active_turn_token') IS NOT NULL")


def literal(c):
    grouped = {}
    for row in c.execute(LITERAL).fetchall():
        try:
            entry = json.loads(row['entry_json'] or '{}')
        except ValueError:
            continue
        grouped.setdefault(row['sid'], []).append((row['scope'], entry))
    return {sid: tuple(entries) for sid, entries in grouped.items()}


@pytest.fixture
def store(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    yield db
    db.close()


def seed(c, n):
    c.execute('BEGIN')
    c.executemany('INSERT INTO gateway_routing VALUES (?,?,?,?)', [
        ('s', f'old{i}', json.dumps({'session_id': f'chat{i}', 'extra': 'x' * 200}), 0)
        for i in range(n)])
    for scope, token in [('z', 't1'), ('a', 't2')]:
        c.execute('INSERT INTO gateway_routing VALUES (?,?,?,?)',
                  (scope, 'live', json.dumps({'session_id': 'chat', 'active_turn_token': token}), 0))
    c.execute('INSERT INTO gateway_routing VALUES (?,?,?,?)',
              ('middle', 'live', json.dumps({'session_id': 'other', 'active_turn_token': 't3'}), 0))
    c.commit()


def test_sparse_scan_vm_work_does_not_grow_with_inactive_history(store):
    c = store._conn
    seed(c, 6000)
    expected = literal(c)
    work = [0]
    def step():
        work[0] += 1
        return 0
    c.set_progress_handler(step, 1)
    try:
        assert _scan(c) == expected
    finally:
        c.set_progress_handler(None, 0)
    assert work[0] < 500, work[0]


@pytest.mark.parametrize('reverse', [0, 1])
@pytest.mark.parametrize('analyze', [False, True])
def test_literal_order_and_mutations(store, reverse, analyze):
    c = store._conn
    seed(c, 30)
    c.execute(f'PRAGMA reverse_unordered_selects={reverse}')
    if analyze:
        c.execute('ANALYZE')
    assert list(_scan(c).items()) == list(literal(c).items())
    c.execute("UPDATE gateway_routing SET entry_json=? WHERE scope='s' AND session_key='old0'",
              (json.dumps({'session_id': 'chat', 'active_turn_token': False}),))
    assert list(_scan(c).items()) == list(literal(c).items())
    c.execute("UPDATE gateway_routing SET entry_json='{}' WHERE scope='z'")
    c.execute("DELETE FROM gateway_routing WHERE scope='a'")
    assert list(_scan(c).items()) == list(literal(c).items())


@pytest.mark.parametrize('raw', ['{broken', b'not json'])
def test_malformed_rows_remain_writable_but_reader_still_raises(store, raw):
    c = store._conn
    seed(c, 3)
    c.execute('INSERT INTO gateway_routing VALUES (?,?,?,?)', ('bad', 'bad', raw, 0))
    for read in (literal, _scan):
        with pytest.raises(sqlite3.OperationalError, match='malformed JSON'):
            read(c)


@pytest.mark.parametrize('raw', [
    '{"active_turn_token":null,"active_turn_token":"last","session_id":"chat"}',
    '{"active_turn_token":"first","active_turn_token":null,"session_id":"chat"}',
    'null', '12', '[]', '{"session_id":null,"active_turn_token":0}',
    '{"active_turn_token":1,}',  # JSON5: SQLite accepts it, Python rejects it.
    b'{"session_id":"chat","active_turn_token":"token"}',
])
def test_literal_json_scalar_duplicate_and_blob_parity(store, raw):
    c = store._conn
    c.execute('INSERT INTO gateway_routing VALUES (?,?,?,?)', ('s', 'k', raw, 0))
    assert list(_scan(c).items()) == list(literal(c).items())


def test_missing_index_legacy_read_only_fallback(store):
    c = store._conn
    seed(c, 2)
    c.execute(f'DROP INDEX IF EXISTS {INDEX}')
    c.execute('PRAGMA query_only=ON')
    assert list(_scan(c).items()) == list(literal(c).items())
    assert not c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (INDEX,)).fetchone()


def test_normal_reopen_installs_index_on_existing_store(store):
    c = store._conn
    seed(c, 2)
    c.execute('INSERT INTO gateway_routing VALUES (?,?,?,?)', ('broken', 'b', '{broken', 0))
    c.execute(f'DROP INDEX IF EXISTS {INDEX}')
    store.close()
    with closing(SessionDB(store.db_path)) as reopened:
        assert reopened._conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (INDEX,)).fetchone()
        reopened._conn.execute("DELETE FROM gateway_routing WHERE scope='broken'")
        assert _scan(reopened._conn) == literal(reopened._conn)


@pytest.mark.parametrize('analyze', [False, True])
def test_index_cannot_reorder_other_routing_readers(store, analyze):
    c = store._conn
    seed(c, 10)
    if analyze:
        c.execute('ANALYZE')
    # The safe CASE predicate is deliberately not implied by old consumers.
    for where in ('1', "json_extract(entry_json, '$.active_turn_token') IS NOT NULL",
                  "scope='s'", "updated_at>=0"):
        with pytest.raises(sqlite3.OperationalError, match='no query solution'):
            c.execute(f'SELECT * FROM gateway_routing INDEXED BY {INDEX} WHERE {where}').fetchall()


@pytest.mark.parametrize('message', ['database is locked', 'malformed JSON', 'no query solution'])
def test_other_operational_errors_do_not_trigger_legacy_fallback(message):
    calls = []
    class Broken:
        def execute(self, sql):
            calls.append(sql)
            raise sqlite3.OperationalError(message)
    with pytest.raises(sqlite3.OperationalError, match=message):
        _scan(Broken())
    assert len(calls) == 1


def test_jsonb_parity_when_supported(store):
    c = store._conn
    try:
        raw = c.execute("SELECT jsonb(?)", ('{"session_id":"chat","active_turn_token":"t"}',)).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if 'no such function' in str(exc):
            pytest.skip('SQLite predates JSONB')
        raise
    c.execute('INSERT INTO gateway_routing VALUES (?,?,?,?)', ('s', 'k', raw, 0))
    assert list(_scan(c).items()) == list(literal(c).items())

