"""Writer journal bounds and supported-shape refusal."""
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_cli import session_throughput_revision as rev


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'state.db'
    store = SessionDB(db_path=path)
    store.close()
    conn = sqlite3.connect(path)
    yield conn
    conn.close()


def test_stamp_does_not_scan_historical_keys(db):
    work = []
    for count in (100, 1000):
        db.executemany("INSERT INTO sessions(id,source,started_at) VALUES (?,'test',1)",
                       ((f'{count}-{i}',) for i in range(count)))
        steps = [0]

        def progress():
            steps[0] += 1
            return 0

        db.set_progress_handler(progress, 1)
        db.execute("UPDATE sessions SET ended_at=2 WHERE id=?", (f'{count}-0',))
        db.set_progress_handler(None, 0)
        work.append(steps[0])
    assert work[1] <= work[0] * 1.5, work


@pytest.mark.parametrize('column', ['rowid', '_rowid_', 'oid'])
def test_rowid_shadow_is_full_only(db, column):
    assert rev.read_stamp(db) is not None
    db.execute(f'ALTER TABLE sessions ADD COLUMN {column} INTEGER')
    assert rev.read_stamp(db) is None


@pytest.mark.parametrize('table,column', [('sessions', 'title'), ('session_model_usage', 'model')])
@pytest.mark.parametrize('collation', ['NOCASE', 'RTRIM'])
def test_declared_collation_cannot_hide_behind_binary_index(db, table, column, collation):
    other = sqlite3.connect(':memory:')
    try:
        for name in ('sessions', 'session_model_usage', 'session_admissions', 'worker_executions'):
            ddl = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
            if name == table:
                ddl = ddl.replace(f'{column} TEXT', f'{column} TEXT COLLATE {collation}')
                # The index collation deliberately masks the column collation.
                if column == 'model':
                    ddl = ddl.replace('PRIMARY KEY (session_id, model,',
                                      'PRIMARY KEY (session_id, model COLLATE BINARY,')
            other.execute(ddl)
        other.execute('CREATE UNIQUE INDEX idx_sessions_title_unique '
                      'ON sessions(title COLLATE BINARY) WHERE title IS NOT NULL')
        assert not rev.install(other)
    finally:
        other.close()


def test_partial_title_predicate_must_be_exact(db):
    db.execute('DROP INDEX idx_sessions_title_unique')
    db.execute('CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions(title) '
               'WHERE title IS NOT NULL AND title != \'excluded\'')
    assert rev.read_stamp(db) is None


@pytest.mark.parametrize('column,value', [('rev', 'bad'), ('floor', 'bad'), ('keys', 'bad'),
                                         ('epoch', b'x'), ('nonce', b'x')])
def test_corrupt_stamp_falls_back_without_exception(db, column, value):
    db.execute(f'UPDATE throughput_revision SET {column}=?', (value,))
    if column == 'nonce':
        db.execute('UPDATE throughput_anchors SET nonce=?', (value,))
    assert rev.read_stamp(db) is None


def test_rotation_cap_rollover_and_ring_wrap(db, monkeypatch):
    # Small constants exercise the exact generated production SQL, not a
    # hand-written substitute, without hundreds of thousands of writes.
    monkeypatch.setattr(rev, 'KEY_CAP', 3)
    monkeypatch.setattr(rev, 'RING_SIZE', 4)
    monkeypatch.setattr(rev, 'REV_LIMIT', 12)
    monkeypatch.setattr(rev, '_TRIGGERS', rev._build_triggers())
    assert rev.install(db)
    first = rev.read_stamp(db)
    assert first is not None
    for i in range(20):
        db.execute("INSERT INTO sessions(id,source,started_at) VALUES (?,'test',1)", (str(i),))
        stamp = rev.read_stamp(db)
        assert stamp is not None
        actual_keys = db.execute('SELECT COUNT(*) FROM throughput_changes').fetchone()[0]
        keys = db.execute('SELECT keys FROM throughput_revision').fetchone()[0]
        assert actual_keys == keys <= 3
        assert db.execute('SELECT COUNT(*) FROM throughput_anchors').fetchone()[0] <= 4
        assert rev.changes_since(db, stamp[2] - 1) == [str(i)]
    assert stamp[1] != first[1]
    assert stamp[2] < 20  # revision rollover really ran
    assert not rev.anchor_matches(db, first[2], first[3])


def test_empty_journal_requires_floor_and_zero_keys(db):
    db.execute("INSERT INTO sessions(id,source,started_at) VALUES ('a','test',1)")
    assert rev.read_stamp(db) is not None
    db.execute('DELETE FROM throughput_changes')
    assert rev.read_stamp(db) is None
    db.execute('UPDATE throughput_revision SET floor=rev')
    assert rev.read_stamp(db) is None  # nonzero keys is independently unsafe
    db.execute('UPDATE throughput_revision SET keys=0')
    assert rev.read_stamp(db) is not None
    db.execute("UPDATE sessions SET ended_at=1 WHERE id='a'")
    assert rev.read_stamp(db) is not None
    assert db.execute('SELECT keys FROM throughput_revision').fetchone()[0] == 1


def test_future_protocol_is_not_rewritten(db):
    db.execute('UPDATE throughput_revision SET version=2')
    before = db.execute('SELECT * FROM throughput_revision').fetchall()
    assert not rev.install(db)
    assert rev.read_stamp(db) is None
    assert db.execute('SELECT * FROM throughput_revision').fetchall() == before


def test_transient_install_failure_retries_on_memoized_open(tmp_path, monkeypatch):
    import hermes_state_schema_memo as memo
    from types import SimpleNamespace

    path = tmp_path / 'retry.db'
    memo.clear()
    now = [1000.0]
    monkeypatch.setattr(memo, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    original = rev.install
    calls = []

    def fail_once(conn):
        calls.append(1)
        return False if len(calls) == 1 else original(conn)

    monkeypatch.setattr(rev, 'install', fail_once)
    first = SessionDB(db_path=path)
    first.close()
    now[0] += memo.HEAL_RETRY_S
    second = SessionDB(db_path=path)
    try:
        assert rev.read_stamp(second._conn) is not None
        assert len(calls) == 2
    finally:
        second.close()
        memo.clear()


@pytest.mark.parametrize('future', [False, True])
def test_unsupported_and_future_are_settled_without_writes(db, future):
    if future:
        db.execute('UPDATE throughput_revision SET version=2')
    else:
        db.execute('ALTER TABLE sessions ADD COLUMN rowid INTEGER')
    db.commit()
    before = db.total_changes
    assert rev.settle_schema(db)
    assert db.total_changes == before
    assert rev.read_stamp(db) is None


def test_install_failure_rolls_back_all_protocol_ddl(db):
    rev.drop_triggers(db)
    for name in rev._TABLE_SQL:
        db.execute(f'DROP TABLE {name}')
    db.commit()

    def refuse_trigger(action, arg1, arg2, database, source):
        if action == sqlite3.SQLITE_CREATE_TRIGGER and arg1 == 'throughput_usage_ai':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(refuse_trigger)
    assert not rev.install(db)
    db.set_authorizer(None)
    assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'throughput_%'").fetchall() == []
    assert rev.install(db)
    assert rev.read_stamp(db) is not None


def test_install_repairs_missing_current_anchor(db):
    db.execute("INSERT INTO sessions(id,source,started_at) VALUES ('a','test',1)")
    before = rev.read_stamp(db)
    db.execute('DELETE FROM throughput_anchors')
    assert rev.read_stamp(db) is None
    assert rev.install(db)
    after = rev.read_stamp(db)
    assert after is not None
    assert after[1] != before[1]


def test_missing_title_index_is_full_only(db):
    assert rev.read_stamp(db) is not None
    db.execute('DROP INDEX idx_sessions_title_unique')
    assert rev.read_stamp(db) is None
