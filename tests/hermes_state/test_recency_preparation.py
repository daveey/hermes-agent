"""No owner-open migration; explicit preparation is bounded, resumable and atomic."""
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
import hermes_state_recency as recency
import hermes_state_recency_build as build
import hermes_state_schema_memo as memo
from tests.hermes_state import test_recency_hot_rows as helpers
from tests.hermes_state.test_recency_cold_index import prepare


def rows(db, sql):
    with db._read_ctx() as conn:
        return [tuple(row) for row in conn.execute(sql).fetchall()]


def state(db):
    return rows(db, 'SELECT cursor, ready FROM message_recency_build')


def assert_mirror(db):
    assert rows(db, 'SELECT id, session_id, timestamp, typeof(timestamp) FROM message_recency_candidates ORDER BY id') == rows(
        db, 'SELECT id, session_id, timestamp, typeof(timestamp) FROM messages ORDER BY id')


def test_owner_open_never_builds_messages_recency_index(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    helpers._seed(db, 1000)
    helpers._write(db, 'DROP INDEX IF EXISTS idx_messages_recency_candidates')
    db.close()
    memo.clear()
    original = SessionDB._open_writer_conn
    attempted = []
    def connect(self):
        conn = original(self)
        def authorize(action, name, table, database, source):
            if action == sqlite3.SQLITE_CREATE_INDEX and name == 'idx_messages_recency_candidates':
                attempted.append(name)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    monkeypatch.setattr(SessionDB, '_open_writer_conn', connect)
    owner = SessionDB(db_path=path)
    try:
        assert not attempted
        assert rows(owner, "SELECT name FROM sqlite_master WHERE name LIKE 'message_recency_%'") == []
    finally:
        owner.close()


def test_batches_resume_across_owners_and_concurrent_changes(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    try:
        helpers._seed(db, 200)
        assert not build.prepare_batch(db, batch_size=3)
        assert state(db) == [(3, 0)]
        peer = SessionDB(db_path=path)
        try:
            helpers._write(peer, 'UPDATE messages SET timestamp=timestamp+1000 WHERE id=1')
            helpers._write(peer, 'DELETE FROM messages WHERE id=2')
            helpers._write(peer, 'UPDATE messages SET id=-9223372036854775808 WHERE id=3')
            helpers._write(peer, "INSERT INTO messages(id, session_id, role, content, timestamp) VALUES(2, 's00000', 'user', 'new', 123)")
            prepare(peer)
            assert state(peer)[0][1] == 1
            assert_mirror(peer)
        finally:
            peer.close()
        # Raw SQL writes, not just facade calls, update the published table.
        helpers._write(db, "UPDATE messages SET session_id='s00001', timestamp='bad' WHERE id=2")
        helpers._write(db, 'DELETE FROM messages WHERE id=1')
        assert_mirror(db)
    finally:
        db.close()


def test_peer_owner_can_initialize_and_write_between_unfinished_batches(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    errors = []
    done = threading.Event()

    def peer_work():
        try:
            peer = SessionDB(db_path=path)
            try:
                helpers._write(peer, 'UPDATE messages SET timestamp=timestamp+1000 WHERE id=1')
            finally:
                peer.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    peer = threading.Thread(target=peer_work)
    try:
        helpers._seed(db, 200)
        assert not build.prepare_batch(db, batch_size=1)
        # Preparation has not completed; a real other owner must not wait for
        # the remaining table build before initialization and ordinary writes.
        peer.start()
        assert done.wait(5), 'unfinished preparation excluded another owner'
        peer.join()
        assert not errors, errors
        assert state(db) == [(1, 0)]
        prepare(db)
        assert_mirror(db)
    finally:
        if peer.ident is not None:
            peer.join(25)
        db.close()


def test_minimum_rowid_is_backfilled(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        helpers._seed(db, 2)
        helpers._write(db, 'UPDATE messages SET id=-9223372036854775808 WHERE id=1')
        prepare(db)
        assert_mirror(db)
    finally:
        db.close()


@pytest.mark.parametrize('installed', [False, True])
def test_interrupted_batch_rolls_back_install_rows_and_cursor(tmp_path, monkeypatch, installed):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        helpers._seed(db, 200)
        if installed:
            assert not build.prepare_batch(db, batch_size=1)
            before = state(db), rows(db, 'SELECT * FROM message_recency_candidates')
        ticks = iter(range(10000))
        monkeypatch.setattr(build, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
        assert not build.prepare_batch(db, transaction_seconds=0.01)
        if installed:
            assert (state(db), rows(db, 'SELECT * FROM message_recency_candidates')) == before
        else:
            assert rows(db, "SELECT name FROM sqlite_master WHERE name LIKE 'message_recency_%'") == []
        monkeypatch.undo()
        prepare(db)
        assert_mirror(db)
        # The connection's temporary progress handler was cleared, including on abort.
        helpers._write(db, 'UPDATE messages SET timestamp=timestamp+1')
        assert_mirror(db)
    finally:
        db.close()


@pytest.mark.parametrize('marker_sql', [
    'UPDATE message_recency_build SET ready=0',
    'UPDATE message_recency_build SET ready=2',
    'DELETE FROM message_recency_build',
])
def test_incomplete_table_is_never_used_as_complete(tmp_path, monkeypatch, marker_sql):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        helpers._seed(db, 200)
        prepare(db)
        helpers._write(db, marker_sql)
        recency.clear()
        actual = helpers._page(db, limit=50)
        assert recency._store_for(str(db.db_path)).watermark is not None
        assert actual == helpers._exact(db, monkeypatch, limit=50)
    finally:
        db.close()
        recency.clear()


def test_empty_store_and_repeated_preparation(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        assert build.prepare_batch(db)
        assert build.prepare_batch(db)
        helpers._seed(db, 20)
        assert_mirror(db)
        assert state(db) == [(None, 1)]
    finally:
        db.close()


def test_preparation_rejects_readonly_and_invalid_budgets(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    try:
        with pytest.raises(ValueError, match='positive'):
            build.prepare_batch(db, batch_size=0)
        with pytest.raises(ValueError, match='positive'):
            build.prepare_batch(db, transaction_seconds=0)
        reader = SessionDB(db_path=path, read_only=True)
        try:
            with pytest.raises(ValueError, match='owner'):
                build.prepare_batch(reader)
        finally:
            reader.close()
    finally:
        db.close()
