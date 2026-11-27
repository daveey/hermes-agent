"""Exact fresh sidebar reads and deterministic work budgets. [daveey]"""
import random
import sqlite3

import pytest

import hermes_state_pinned_backfill as pins
from hermes_state import SessionDB
from hermes_state_local import exclude_superseded_segments
from hermes_state_sessions import _session_filter_where, _where_sql


@pytest.fixture
def db(tmp_path):
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def seed(db, n=180):
    rng = random.Random(817)
    rows = []
    for i in range(n):
        parent = f"s{i - 1}" if i and i % 3 == 0 else None
        config = rng.choice([None, '{}', '{broken', '{"_delegate_from":"s0"}',
                             '{"_branched_from":"s0"}', '{"_reset_from":"s0"}',
                             '{"_delegate_from":null}', '{"\\u005fdelegate_from":false}'])
        rows.append((f"s{i}", rng.choice(['cli', 'cron', 'telegram', 'local', 'tool', '']),
                     1700000000 + i // 2, rng.randrange(4), rng.randrange(2), rng.randrange(2),
                     rng.randrange(2), parent, config, rng.choice(['compression', 'branched', None]),
                     rng.choice(['desktop', 'cron', None])))
    def write(conn):
        conn.executemany("INSERT INTO sessions (id,source,started_at,message_count,pinned,archived,hidden,"
                         "parent_session_id,model_config,end_reason,created_source) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO messages(session_id,role,content,timestamp) "
                     "SELECT id,'assistant','answer',started_at+1 FROM sessions")
    db._execute_write(write)


def old_count(db, kw):
    where, params = _session_filter_where(**kw)
    if kw.get('exclude_children'):
        exclude_superseded_segments(db, where, params)
    return db._read_one('SELECT COUNT(*) FROM sessions s' + _where_sql(where, ' '), params)[0]


def page(db, **kw):
    return db.list_sessions_rich(include_pinned=True, **kw)


@pytest.mark.parametrize('seed_value', [1, 7, 23])
def test_seeded_full_output_and_count_equivalence(db, monkeypatch, seed_value):
    seed(db)
    rng = random.Random(seed_value)
    for _ in range(45):
        kw = dict(source=rng.choice([None, 'cron']),
                  exclude_sources=rng.choice([None, ['cron', 'tool'], ['cli'], ['missing'], [None]]),
                  min_message_count=rng.randrange(4), include_archived=rng.choice([False, True]),
                  archived_only=rng.choice([False, True]), include_subagents=rng.choice([False, True]),
                  local_created_sources=rng.choice([None, ['desktop']]),
                  exclude_source_pairs=rng.choice([None, [('local', 'cron')]]))
        count_kw = dict(kw, exclude_children=rng.choice([False, True]))
        assert db.session_count(**count_kw) == old_count(db, count_kw)
        list_kw = dict(kw, include_children=not count_kw['exclude_children'],
                       limit=rng.randrange(1, 12), offset=rng.randrange(4),
                       compact_rows=rng.choice([False, True]), order_by_last_active=rng.choice([False, True]))
        monkeypatch.setattr(pins, 'ENABLED', False)
        expected = page(db, **list_kw)
        monkeypatch.setattr(pins, 'ENABLED', True)
        assert page(db, **list_kw) == expected


@pytest.mark.parametrize('inventory', [6000, 18000])
def test_count_excluded_inventory_vm_budget(db, monkeypatch, inventory):
    def write(conn):
        conn.executemany("INSERT INTO sessions(id,source,started_at,message_count) VALUES (?,'tool',1700000000,1)",
                         [(f'x{i}',) for i in range(inventory)])
        conn.execute("INSERT INTO sessions(id,source,started_at,message_count) VALUES ('keep','desktop',1700000000,1)")
    db._execute_write(write)
    steps = [0]
    original = db._read_one
    def read(sql, params=()):
        if 'COUNT(*) FROM sessions s' not in sql:
            return original(sql, params)
        with db._read_ctx() as conn:
            conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1)
            try:
                return conn.execute(sql, params).fetchone()
            finally:
                conn.set_progress_handler(None, 0)
    monkeypatch.setattr(db, '_read_one', read)
    assert db.session_count(exclude_sources=['tool'], min_message_count=1) == 1
    assert steps[0] < 2000, steps


def test_count_admission_index_vm_budget(db, monkeypatch):
    seed(db, 900)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET source='cron', parent_session_id=NULL,"
                                        "model_config='{}', archived=0, message_count=1"))
    costs = []
    original = db._read_one
    def read(sql, params=()):
        if 'COUNT(*) FROM sessions s' not in sql:
            return original(sql, params)
        steps = [0]
        with db._read_ctx() as conn:
            conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1)
            try:
                result = conn.execute(sql, params).fetchone()
            finally:
                conn.set_progress_handler(None, 0)
        costs.append(steps[0])
        return result
    monkeypatch.setattr(db, '_read_one', read)
    kw = dict(source='cron', exclude_children=True, min_message_count=1)
    assert db.session_count(**kw) == old_count(db, kw) == 900
    assert costs[0] < costs[1] * 0.7, costs


def test_pins_already_on_page_do_not_hydrate_twice(db, monkeypatch):
    seed(db, 12)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=1,archived=0,hidden=0,"
                                        "parent_session_id=NULL,model_config=NULL"))
    import hermes_state_page_pack as pack
    calls = []
    original = pack.read_chunks
    def chunks(*args):
        calls.append(args[1])
        return original(*args)
    monkeypatch.setattr(pack, 'read_chunks', chunks)
    assert len(page(db, limit=20)) == 12
    assert not calls


def test_next_call_sees_pin_archive_message_and_threshold_changes(db):
    for sid in ['old', 'new']:
        db.create_session(sid, source='desktop')
    db._execute_write(lambda c: c.execute("UPDATE sessions SET started_at=1700000000, message_count=1"))
    db._execute_write(lambda c: c.execute("UPDATE sessions SET started_at=1700000100 WHERE id='new'"))
    opts = dict(limit=1, min_message_count=1, order_by_last_active=True)
    assert [r['id'] for r in page(db, **opts)] == ['new']
    db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=1 WHERE id='old'"))
    assert [r['id'] for r in page(db, **opts)] == ['new', 'old']
    db._execute_write(lambda c: c.execute("UPDATE sessions SET archived=1 WHERE id='old'"))
    assert page(db, **opts)[1]['archived'] == 1
    db._execute_write(lambda c: c.execute("UPDATE sessions SET archived=0 WHERE id='old'"))
    assert page(db, **opts)[1]['archived'] == 0
    db.append_message('old', role='user', content='new activity')
    assert page(db, **opts)[0]['id'] == 'old'
    db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=0, last_activity_at=NULL WHERE id='old'"))
    db._execute_write(lambda c: c.execute("DELETE FROM messages WHERE session_id='old'"))
    assert [r['id'] for r in page(db, **opts)] == ['new']
    db._execute_write(lambda c: c.execute("UPDATE sessions SET message_count=0 WHERE id='new'"))
    assert page(db, **opts)[0]['id'] == 'old'
    count_opts = dict(exclude_sources=['tool'], min_message_count=1)
    assert db.session_count(**count_opts) == 1
    db._execute_write(lambda c: c.execute("UPDATE sessions SET message_count=1 WHERE id='new'"))
    assert db.session_count(**count_opts) == 2
    assert page(db, **opts)[0]['id'] == 'new'


@pytest.mark.parametrize('outer_transaction', [False, True])
def test_backfill_transaction_and_ioerr_retry(db, monkeypatch, outer_transaction):
    db.create_session('pin', source='desktop')
    db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=1"))
    import hermes_state_page_pack as pack
    original = pack.read_chunks
    attempts = []
    def chunks(conn, sql, params):
        attempts.append(conn)
        assert conn.in_transaction
        if len(attempts) == 1:
            raise sqlite3.OperationalError('disk I/O error')
        return original(conn, sql, params)
    monkeypatch.setattr(pack, 'read_chunks', chunks)
    with db._read_ctx() as conn:
        if outer_transaction:
            conn.execute('BEGIN')
        # [daveey] Bind the actual retry wrapper to the same borrowed connection.
        from contextlib import nullcontext
        monkeypatch.setattr(db, '_read_ctx', lambda: nullcontext(conn))
        rows = []
        pins.backfill_pins(db, rows, 'SELECT s.id, ', '', ['s.pinned=1'], [])
        assert [r['id'] for r in rows] == ['pin']
        assert len(attempts) == 2
        assert conn.in_transaction is outer_transaction
        if outer_transaction:
            conn.execute('ROLLBACK')


@pytest.mark.parametrize('identity', [b'pin', None])
def test_pin_identity_transport_falls_back_exactly(db, monkeypatch, identity):
    if identity is not None:
        db._execute_write(lambda c: c.execute(
            "INSERT INTO sessions(id,source,started_at,pinned) VALUES (?,'desktop',1700000000,1)", (identity,)))
    db.create_session('other', source='desktop')
    db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=1"))
    expected = [{'id': identity}]
    actual = [{'id': identity}]
    monkeypatch.setattr(pins, 'ENABLED', False)
    pins.backfill_pins(db, expected, 'SELECT s.id, ', '', ['s.pinned=1'], [])
    monkeypatch.setattr(pins, 'ENABLED', True)
    pins.backfill_pins(db, actual, 'SELECT s.id, ', '', ['s.pinned=1'], [])
    assert actual == expected


def test_pin_probe_and_hydration_share_snapshot(db, monkeypatch):
    db.create_session('pin', source='desktop')
    db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=1"))
    import hermes_state_page_pack as pack
    original = pack.read_chunks
    commits = []
    def chunks(conn, sql, params):
        db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=0"))
        commits.append(True)
        return original(conn, sql, params)
    monkeypatch.setattr(pack, 'read_chunks', chunks)
    rows = []
    pins.backfill_pins(db, rows, 'SELECT s.id, ', '', ['s.pinned=1'], [])
    assert commits == [True] and [r['id'] for r in rows] == ['pin']
    fresh = []
    pins.backfill_pins(db, fresh, 'SELECT s.id, ', '', ['s.pinned=1'], [])
    assert fresh == []
    with db._read_ctx() as conn:
        assert not conn.in_transaction


def test_pin_hydration_failure_releases_owned_snapshot(db, monkeypatch):
    db.create_session('pin', source='desktop')
    db._execute_write(lambda c: c.execute("UPDATE sessions SET pinned=1"))
    import hermes_state_page_pack as pack
    borrowed = []
    def fail(conn, *_):
        borrowed.append(conn)
        assert conn.in_transaction
        raise ValueError('decode failure')
    monkeypatch.setattr(pack, 'read_chunks', fail)
    with pytest.raises(ValueError, match='decode failure'):
        pins.backfill_pins(db, [], 'SELECT s.id, ', '', ['s.pinned=1'], [])
    assert len(borrowed) == 1 and not borrowed[0].in_transaction


def test_count_readonly_missing_indexes_and_sqlite_types(db):
    seed(db, 30)
    db._execute_write(lambda c: c.execute("UPDATE sessions SET source=? WHERE id='s1'", (b'bytes',)))
    db._execute_write(lambda c: c.execute('ANALYZE'))
    for exclude in [['cron'], [''], [b'bytes'], [None], ['missing', 'tool']]:
        kw = dict(exclude_sources=exclude, exclude_children=True, min_message_count=1)
        assert db.session_count(**kw) == old_count(db, kw)
    db._execute_write(lambda c: c.execute('DROP INDEX idx_sessions_count_admission'))
    db._execute_write(lambda c: c.execute('DROP INDEX idx_sessions_source'))
    db._execute_write(lambda c: c.execute('DROP INDEX idx_sessions_source_id'))
    with SessionDB(db_path=db.db_path, read_only=True) as reader:
        kw = dict(exclude_sources=['cron', 'tool'], exclude_children=True)
        assert reader.session_count(**kw) == old_count(reader, kw)
        assert reader._read_one("SELECT 1 FROM sqlite_master WHERE name='idx_sessions_count_admission'") is None


def test_count_index_migration_is_idempotent_and_opt_in(db):
    seed(db, 60)
    db._execute_write(lambda c: c.execute('DROP INDEX idx_sessions_count_admission'))
    expected = page(db, limit=15)
    path = db.db_path
    db.close()
    with SessionDB(db_path=path) as upgraded:
        assert page(upgraded, limit=15) == expected
        before = upgraded._read_one('PRAGMA schema_version')[0]
        for analyze in [False, True]:
            if analyze:
                upgraded._execute_write(lambda c: c.execute('ANALYZE'))
            for suffix in ['', " WHERE source='cli'", ' WHERE archived=0', ' WHERE id IS NOT NULL']:
                with pytest.raises(sqlite3.OperationalError, match='no query solution'):
                    upgraded._read_one('SELECT id FROM sessions INDEXED BY idx_sessions_count_admission'
                                       + suffix + ' ORDER BY started_at DESC LIMIT 5')
        after = upgraded._read_one('PRAGMA schema_version')[0]
        assert after >= before
    with SessionDB(db_path=path) as reopened:
        assert reopened._read_one('PRAGMA schema_version')[0] == after
        assert page(reopened, limit=15) == expected
