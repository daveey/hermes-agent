"""Isolate protocol refusal boundaries so adjacent fences cannot mask them."""
import sqlite3

import pytest

from hermes_cli import session_throughput_revision as rev
from tests.hermes_cli import test_throughput_revision as helpers

db = helpers.db


@pytest.mark.parametrize('sql', [
    'UPDATE throughput_revision SET floor=-1',
    'UPDATE throughput_revision SET floor=rev+1',
    'UPDATE throughput_revision SET keys=-1',
    f'UPDATE throughput_revision SET keys={rev.KEY_CAP + 1}',
    'UPDATE throughput_revision SET rev=-1, floor=-1',
    'UPDATE throughput_changes SET rev=rev+1',
    'DELETE FROM throughput_revision',
    'UPDATE throughput_revision SET version=2',
])
def test_journal_invariant_refuses_invalid_state(db, sql):
    db.execute("INSERT INTO sessions(id,source,started_at) VALUES ('a','test',1)")
    assert rev._invariant_ok(db)
    db.execute(sql)
    assert not rev._invariant_ok(db)


@pytest.mark.parametrize('column,value', [
    ('rev', 'bad'), ('floor', 'bad'), ('keys', 'bad'),
    ('epoch', '0123456789abcdef'), ('nonce', '0123456789abcdef'),
    ('epoch', b'x'), ('nonce', b'x'),
])
def test_journal_type_and_blob_guards_direct(db, column, value):
    db.execute(f'UPDATE throughput_revision SET {column}=?', (value,))
    assert not rev._invariant_ok(db)


@pytest.mark.parametrize('mutation', ['missing', 'definition', 'index'])
def test_protocol_tables_must_be_exact(db, mutation):
    if mutation == 'missing':
        db.execute('DROP TABLE throughput_anchors')
    elif mutation == 'definition':
        db.execute('ALTER TABLE throughput_anchors ADD COLUMN alien TEXT')
    else:
        db.execute('DROP INDEX throughput_changes_rev')
        db.execute('CREATE INDEX throughput_changes_rev ON throughput_changes(session_id)')
    assert rev._tables_exact(db) is None
    before = db.total_changes
    assert not rev.install(db)
    assert rev.settle_schema(db)
    assert db.total_changes == before
    assert rev.read_stamp(db) is None


@pytest.mark.parametrize('table', ['session_admissions', 'worker_executions'])
def test_required_schema_marker(db, table):
    db.execute(f'DROP TABLE {table}')
    assert not rev._supported_shape(db)


@pytest.mark.parametrize('change', [
    'id_type', 'id_pk', 'ended_at', 'title', 'compound_session_pk',
    'usage_type', 'usage_nullable', 'usage_pk', 'without_rowid',
])
def test_column_shapes(db, change):
    other = sqlite3.connect(':memory:')
    try:
        for table in ('sessions', 'session_model_usage', 'session_admissions', 'worker_executions'):
            ddl = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
            replacements = {
                ('sessions', 'id_type'): ('id TEXT PRIMARY KEY', 'id BLOB PRIMARY KEY'),
                ('sessions', 'id_pk'): ('id TEXT PRIMARY KEY', 'id TEXT'),
                ('sessions', 'ended_at'): ('ended_at', 'ended_other'),
                ('sessions', 'title'): ('title TEXT', 'other_title TEXT'),
                ('sessions', 'compound_session_pk'): ('id TEXT PRIMARY KEY', 'id TEXT'),
                ('session_model_usage', 'usage_type'): ('input_tokens INTEGER', 'input_tokens REAL'),
                ('session_model_usage', 'usage_nullable'): ('model TEXT NOT NULL', 'model TEXT'),
                ('session_model_usage', 'usage_pk'): ('billing_mode, task)', 'billing_mode)'),
            }
            replacement = replacements.get((table, change))
            if replacement:
                ddl = ddl.replace(*replacement, 1)
            if table == 'sessions' and change == 'compound_session_pk':
                ddl = ddl.rstrip()[:-1] + ', PRIMARY KEY(id, source))'
            if table == 'sessions' and change == 'without_rowid':
                ddl += ' WITHOUT ROWID'
            other.execute(ddl)
        title = 'other_title' if change == 'title' else 'title'
        other.execute(f'CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions({title}) WHERE {title} IS NOT NULL')
        assert not rev._supported_shape(other), change
    finally:
        other.close()


@pytest.mark.parametrize('sql', [
    'CREATE UNIQUE INDEX unexpected_unique ON sessions(source)',
    'CREATE UNIQUE INDEX expression_unique ON sessions(lower(source))',
    'CREATE UNIQUE INDEX extra_usage_unique ON session_model_usage(model)',
])
def test_unexpected_unique_keys(db, sql):
    db.execute(sql)
    assert not rev._supported_shape(db)


def test_missing_stamp_never_reuses_memo(db, monkeypatch):
    from hermes_cli import machine_throughput_incremental as inc

    inc.reset()
    assert inc.read(db, 'fixture', (1, 2)) is not None
    monkeypatch.setattr(rev, 'read_stamp', lambda _: None)
    assert inc.read(db, 'fixture', (1, 2)) is None
    assert 'fixture' not in inc._memos


@pytest.mark.parametrize('lost', [
    ('throughput_anchors',), ('throughput_changes',), ('throughput_revision',),
    ('throughput_anchors', 'throughput_changes'),
    ('throughput_anchors', 'throughput_revision'),
    ('throughput_changes', 'throughput_revision'),
])
def test_partial_protocol_loss_does_not_break_session_writers(db, lost):
    for table in lost:
        db.execute(f'DROP TABLE {table}')
    surviving = {name: db.execute(f'SELECT * FROM {name}').fetchall()
                 for name in rev._TABLE_SQL if name not in lost}
    assert not rev.install(db)
    assert rev.settle_schema(db)
    assert rev.read_stamp(db) is None
    assert {name: db.execute(f'SELECT * FROM {name}').fetchall()
            for name in surviving} == surviving
    db.execute("INSERT INTO sessions(id,source,started_at) VALUES ('after-loss','test',1)")
    db.execute("INSERT INTO session_model_usage(session_id,model,input_tokens) VALUES ('after-loss','m',7)")
    assert db.execute("SELECT id FROM sessions WHERE id='after-loss'").fetchone() == ('after-loss',)


@pytest.mark.parametrize('foreign', ['future', 'table', 'index', 'trigger', 'view'])
def test_partial_cleanup_preserves_foreign_schema(db, foreign):
    db.execute('DROP TABLE throughput_anchors')
    statements = {
        'future': ['UPDATE throughput_revision SET version=2'],
        'table': ['ALTER TABLE throughput_revision ADD COLUMN alien TEXT'],
        'index': ['DROP INDEX throughput_changes_rev',
                  'CREATE INDEX throughput_changes_rev ON throughput_changes(session_id)'],
        'trigger': ['DROP TRIGGER throughput_sessions_ai',
                    'CREATE TRIGGER throughput_sessions_ai AFTER INSERT ON sessions BEGIN SELECT 1; END'],
        'view': ['CREATE VIEW throughput_anchors AS SELECT 1 AS slot'],
    }
    for statement in statements[foreign]:
        db.execute(statement)
    before = db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
    assert rev.settle_schema(db)
    assert db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall() == before


def test_partial_cleanup_failure_is_atomic_and_retryable(db):
    db.execute('DROP TABLE throughput_anchors')
    before = rev._master(db, 'trigger')

    def refuse_late_drop(action, name, *args):
        if action == sqlite3.SQLITE_DROP_TRIGGER and name == 'throughput_usage_ai':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(refuse_late_drop)
    assert not rev.settle_schema(db)
    db.set_authorizer(None)
    assert rev._master(db, 'trigger') == before
    assert rev.settle_schema(db)
    db.execute("INSERT INTO sessions(id,source,started_at) VALUES ('recovered','test',1)")


def test_compound_pk_column_guard_independently(db, monkeypatch):
    original = rev._columns

    def columns(conn, table):
        result = original(conn, table)
        if table == 'sessions':
            typ, nullable, _ = result['source']
            result['source'] = (typ, nullable, 2)
        return result

    # Isolate the column guard from the redundant UNIQUE-index shape guard.
    monkeypatch.setattr(rev, '_columns', columns)
    assert not rev._supported_shape(db)


@pytest.mark.parametrize('layout', ['complete', 'absent'])
def test_partial_cleanup_requires_partial_layout(db, layout):
    from hermes_cli.session_throughput_repair import disable_stranded

    if layout == 'absent':
        for table in rev._TABLE_SQL:
            db.execute(f'DROP TABLE {table}')
    before = rev._master(db, 'trigger')
    assert disable_stranded(db) is None
    assert rev._master(db, 'trigger') == before


@pytest.mark.parametrize('version', [None, 'future', 2])
def test_partial_cleanup_requires_known_single_version(db, version):
    from hermes_cli.session_throughput_repair import disable_stranded

    db.execute('DROP TABLE throughput_anchors')
    if version is None:
        db.execute('DELETE FROM throughput_revision')
    else:
        db.execute('UPDATE throughput_revision SET version=?', (version,))
    before = rev._master(db, 'trigger')
    assert disable_stranded(db) is None
    assert rev._master(db, 'trigger') == before


def test_partial_cleanup_closed_connection_is_retryable():
    from hermes_cli.session_throughput_repair import disable_stranded

    conn = sqlite3.connect(':memory:')
    conn.close()
    assert disable_stranded(conn) is False


def test_empty_journal_floor_independently(db):
    db.execute("INSERT INTO sessions(id,source,started_at) VALUES ('a','test',1)")
    db.execute('DELETE FROM throughput_changes')
    db.execute('UPDATE throughput_revision SET keys=0')
    assert not rev._invariant_ok(db)


def test_usage_unique_cardinality_independently(db, monkeypatch):
    original = rev._unique_indexes

    def indexes(conn, table):
        rows = original(conn, table)
        if table == 'session_model_usage':
            # Put the valid PK first, unlike SQLite's default new-index-first order.
            return rows + [('alien', 'c', False, [('model', 'BINARY')])]
        return rows

    monkeypatch.setattr(rev, '_unique_indexes', indexes)
    assert not rev._supported_shape(db)


def test_closed_connection_is_unproven():
    conn = sqlite3.connect(':memory:')
    conn.close()
    assert not rev._supported_shape(conn)
    assert not rev.install(conn)
    assert rev.read_stamp(conn) is None
    assert not rev.anchor_matches(conn, 0, b'x' * 16)
