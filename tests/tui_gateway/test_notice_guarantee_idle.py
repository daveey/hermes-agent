"""Notice candidate structure is commit-keyed; aging still advances without writes."""
import contextlib
import sqlite3
import time

import pytest

from tests.tui_gateway import test_notice_guarantee as helpers
from tui_gateway import notice_guarantee as ng

db = helpers.db


def _trace(db):
    statements = []
    db._read_retrying_ioerr(lambda conn: conn.set_trace_callback(statements.append))
    return statements


def _scans(statements):
    return [s for s in statements if 'FROM sessions s' in s]


def test_unchanged_notice_candidates_do_not_rescan(db):
    helpers._notice(db, 'completed')
    now = time.time()
    first = ng.candidates(db, now)
    assert len(first) == 1
    statements = _trace(db)
    assert ng.candidates(db, now + 1) == first
    assert _scans(statements) == []
    # The very next read sees a newly appended answer, no timer expiry needed.
    db.append_message(helpers.CHAT, 'assistant', 'Handled')
    assert ng.candidates(db, now + 2) == []
    assert _scans(statements)


def test_aging_without_writes_does_not_need_a_rescan(db):
    row = helpers._notice(db, 'young', age=0)
    now = time.time()
    db._write_rowcount('UPDATE messages SET timestamp=? WHERE id=?', (now, row))
    assert ng.candidates(db, now) == []
    statements = _trace(db)
    assert [c['msg_id'] for c in ng.candidates(db, now + ng.GRACE_S)] == [row]
    assert ng.candidates(db, now + ng.LOOKBACK_S + 10) == []
    assert _scans(statements) == []


def test_external_commit_invalidates_next_read(db):
    row = helpers._notice(db, 'external')
    now = time.time()
    assert len(ng.candidates(db, now)) == 1
    with contextlib.closing(sqlite3.connect(db.db_path)) as other:
        other.execute('UPDATE messages SET active=0 WHERE id=?', (row,))
        other.commit()
    assert ng.candidates(db, now) == []


def test_same_connection_writes_and_rollback_are_not_cached(tmp_path):
    from tui_gateway import notice_candidates as nc
    with contextlib.closing(sqlite3.connect(tmp_path / 'reader.db')) as conn:
        conn.execute('CREATE TABLE items(value)')
        conn.execute('INSERT INTO items VALUES (1)')
        conn.commit()

        class Reader:
            def _read_retrying_ioerr(self, fn):
                return fn(conn)

        reader = Reader()
        sql = 'SELECT value FROM items'
        assert nc.rows(reader, sql) == [(1,)]
        conn.execute('UPDATE items SET value=2')
        conn.commit()
        assert nc.rows(reader, sql) == [(2,)]
        conn.execute('UPDATE items SET value=3')
        assert nc.rows(reader, sql) == [(3,)]
        conn.rollback()
        assert nc.rows(reader, sql) == [(2,)]


def test_no_trusted_recency_is_excluded(db):
    helpers._notice(db, 'invalid recency')
    db._write_rowcount('UPDATE messages SET timestamp=-1 WHERE session_id=?', (helpers.CHAT,))
    db._write_rowcount('UPDATE sessions SET last_activity_at=NULL, started_at=-1 WHERE id=?', (helpers.CHAT,))
    assert ng.candidates(db, time.time()) == []


def test_real_pinned_snapshot_bypasses_warm_cache(db):
    helpers._notice(db, 'pinned')
    now = time.time()
    first = ng.candidates(db, now)
    statements = _trace(db)
    with db.transcript_read_snapshot() as consistent:
        assert consistent.pinned
        assert ng.candidates(db, now) == first
        assert ng.candidates(db, now) == first
    assert len(_scans(statements)) == 2


def test_real_writer_fallback_observes_own_commits(db, monkeypatch):
    row = helpers._notice(db, 'writer fallback')
    monkeypatch.setattr(db, '_wal_active', False)
    now = time.time()
    assert len(ng.candidates(db, now)) == 1
    statements = _trace(db)
    assert len(ng.candidates(db, now)) == 1
    assert _scans(statements) == []
    db._write_rowcount('UPDATE messages SET active=0 WHERE id=?', (row,))
    assert ng.candidates(db, now) == []
    assert _scans(statements)


def test_raw_reader_fallback_and_failed_query_retry():
    from tui_gateway import notice_candidates as nc

    class RawReader:
        def _read_all(self, sql):
            assert sql == 'SELECT value'
            return [(7,)]

    assert nc.rows(RawReader(), 'SELECT value') == [(7,)]
    with contextlib.closing(sqlite3.connect(':memory:')) as conn:
        class Reader:
            def _read_retrying_ioerr(self, fn):
                return fn(conn)

        reader = Reader()
        with pytest.raises(sqlite3.OperationalError):
            nc.rows(reader, 'SELECT value FROM items')
        conn.execute('CREATE TABLE items(value)')
        conn.execute('INSERT INTO items VALUES (8)')
        conn.commit()
        assert nc.rows(reader, 'SELECT value FROM items') == [(8,)]


def test_cache_is_bounded_and_reopen_and_sql_are_distinct(monkeypatch):
    from collections import OrderedDict
    from tui_gateway import notice_candidates as nc
    monkeypatch.setattr(nc, '_cache', OrderedDict())
    monkeypatch.setattr(nc, '_MAX_READERS', 2)
    with contextlib.ExitStack() as stack:
        class Reader:
            conn: sqlite3.Connection

            def _read_retrying_ioerr(self, fn):
                return fn(self.conn)

        reader = Reader()
        for value in range(4):
            reader.conn = stack.enter_context(contextlib.closing(sqlite3.connect(':memory:')))
            assert nc.rows(reader, f'SELECT {value}') == [(value,)]
            assert nc.rows(reader, 'SELECT 99') == [(99,)]
            assert len(nc._cache) <= 2


def test_full_pass_uses_retained_launch_profile_reader(db, monkeypatch):
    from tui_gateway import server
    import hermes_state_outcome as so
    helpers._notice(db, 'human', blocked_on_human=True, gateway='remote-example')
    monkeypatch.setattr(server, '_readopt_wake_profiles', lambda: [None])
    monkeypatch.setattr(server, '_profile_home', lambda profile: None)
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(ng, '_this_gateway', lambda: 'local-example')
    monkeypatch.setattr(so, '_live_turn', lambda db, sid: False)
    monkeypatch.setattr(ng, '_refresh', lambda db, sid: True)
    assert ng.guarantee_pass(holders=list)[0]['result'] == 'refreshed:human'
    statements = _trace(db)
    assert ng.guarantee_pass(holders=list)[0]['result'] == 'skipped:refreshed'
    assert _scans(statements) == []


def test_text_notice_timestamp_is_ineligible(db):
    row = helpers._notice(db, 'bad time')
    db._write_rowcount('UPDATE messages SET timestamp=? WHERE id=?', ('not-an-epoch', row))
    assert ng.candidates(db, time.time()) == []
