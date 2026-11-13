"""Real SQLite writers must invalidate projections without reading session history."""
import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("s", source="cli")
    server._sessions_db_sig_cache.clear()
    server._sessions_digest_at.clear()
    from tui_gateway import watch_store
    watch_store.reset()
    yield path, db
    watch_store.reset()
    db.close()


def test_settled_unchanged_tick_opens_no_database(store, monkeypatch):
    """Card J: an unchanged store costs one PRAGMA on its kept connection; no open, no projection read."""
    path, _db = store
    before = server._session_db_content_sig(path)
    connect = hermes_state._connect_tracked_db
    calls, statements = [], []

    def observed(*args, **kwargs):
        calls.append(args)
        return connect(*args, **kwargs)

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", observed)
    from tui_gateway import watch_store
    with watch_store.observed(path) as (_key, conn):
        conn.set_trace_callback(statements.append)
    for _ in range(3):
        assert server._session_db_content_sig(path) == before
    assert calls == []
    assert statements == ["PRAGMA data_version"] * 3
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE sessions SET title='next tick'")
    assert server._session_db_content_sig(path) != before


def test_projection_reads_are_bounded_after_unrelated_commits(store, monkeypatch):
    path, _db = store
    statements = []
    connect = hermes_state._connect_tracked_db

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", traced)
    monkeypatch.setattr(server, "SESSION_DIGEST_MIN_S", 0)
    before = server._session_db_content_sig(path)
    with sqlite3.connect(path) as writer:
        writer.execute("INSERT INTO state_meta(key, value) VALUES ('unrelated', '1')")
    assert server._session_db_content_sig(path) == before
    assert server._session_db_content_sig(path) == before
    assert not [s for s in statements if "FROM SESSIONS" in s.upper()]


@pytest.mark.parametrize("field", server._SESSION_SIGNATURE_FIELDS)
def test_every_projection_field_invalidates_on_next_tick(store, field):
    path, _db = store
    before = server._session_db_content_sig(path)
    # Raw sqlite connection models a writer from a release predating the counter.
    with sqlite3.connect(path) as writer:
        value = writer.execute(f'SELECT "{field}" FROM sessions WHERE id = ?', ('s',)).fetchone()[0]
        new = value + 1 if isinstance(value, (int, float)) else "changed"
        writer.execute(f'UPDATE sessions SET "{field}" = ? WHERE id = ?', (new, 's'))
    assert server._session_db_content_sig(path) != before


def test_insert_delete_and_rollback(store):
    path, _db = store
    before = server._session_db_content_sig(path)
    with sqlite3.connect(path) as writer:
        writer.execute("INSERT INTO sessions(id, source, started_at) VALUES ('new', 'cli', 1)")
    inserted = server._session_db_content_sig(path)
    assert inserted != before
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM sessions WHERE id = 'new'")
    deleted = server._session_db_content_sig(path)
    assert deleted != inserted
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE sessions SET title = 'rollback' WHERE id = 's'")
        assert server._session_db_content_sig(path) == deleted
        writer.rollback()
    assert server._session_db_content_sig(path) == deleted


def test_heartbeat_and_equal_updates_do_not_write_counter(store):
    path, _db = store
    with sqlite3.connect(path) as writer:
        before = writer.execute("SELECT value FROM state_meta WHERE key='sessions_change_seq'").fetchone()
        assert before is not None
        changes = writer.total_changes
        stamps = writer.execute("SELECT revision FROM session_change_revision").fetchone()[0]
        writer.execute("UPDATE sessions SET last_activity_at=123, last_activity_description='busy'")
        writer.execute("UPDATE sessions SET title=title, archived=archived")
        # The session change cursor (hermes_state_session_change) does watch a heartbeat: each stamp is two
        # trigger writes (its revision row and the session's row). Nothing else is written.
        stamped = writer.execute("SELECT revision FROM session_change_revision").fetchone()[0] - stamps
        assert stamped >= 1
        assert writer.total_changes - changes - 2 * stamped == 2
        assert writer.execute("SELECT value FROM state_meta WHERE key='sessions_change_seq'").fetchone() == before


def test_first_install_seeds_generation_and_settled_open_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.close()
    with sqlite3.connect(path) as observer:
        seeded = observer.execute("SELECT value FROM state_meta WHERE key='sessions_change_seq'").fetchone()
        assert seeded is not None and len(seeded[0]) == 32
        schema_before = observer.execute("PRAGMA schema_version").fetchone()
        before = server._session_db_content_sig(path)
        assert before[0] == "revision"
        assert before[-1] == seeded[0]
        reopened = SessionDB(path)
        reopened.close()
        assert observer.execute("PRAGMA schema_version").fetchone() == schema_before
        assert server._session_db_content_sig(path) == before
        observer.execute("INSERT INTO sessions(id, source, started_at) VALUES ('first', 'cli', 1)")
    assert server._session_db_content_sig(path) != before


def test_replacement_with_equal_counter_is_not_cached(store, tmp_path):
    path, _db = store
    before = server._session_db_content_sig(path)
    replacement = tmp_path / "replacement.db"
    other = SessionDB(replacement)
    other.create_session("different", source="cli")
    other.close()
    # Force the same generation to exercise the independent inode fence.
    from contextlib import closing

    # A connection context commits but does not close. Close/checkpoint the source
    # before moving its main file, otherwise the generation remains in its WAL.
    with closing(sqlite3.connect(replacement)) as writer:
        writer.execute("UPDATE state_meta SET value=? WHERE key='sessions_change_seq'", (before[-1],))
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _db.close()
    replacement.replace(path)
    after = server._session_db_content_sig(path)
    assert after != before
    assert after[-1] == before[-1]


def test_in_place_restore_of_equal_count_store_invalidates(store, tmp_path):
    path, db = store
    other_path = tmp_path / "other.db"
    other = SessionDB(other_path)
    other.create_session("different", source="cli")
    other.close()
    db.close()
    before = server._session_db_content_sig(path)
    inode = path.stat().st_ino
    with sqlite3.connect(other_path) as source, sqlite3.connect(path) as target:
        source.backup(target)
    assert path.stat().st_ino == inode
    assert server._session_db_content_sig(path) != before


def test_failed_read_retries_without_throttle(store, monkeypatch):
    path, _db = store
    before = server._session_db_content_sig(path)
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE sessions SET title='after failure'")
    connect = hermes_state._connect_tracked_db
    from tui_gateway import watch_store
    watch_store.reset()  # the kept reader is gone: the next pass must open one, and that open fails

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", locked)
    assert server._session_db_content_sig(path) == before
    monkeypatch.setattr(hermes_state, "_connect_tracked_db", connect)
    assert server._session_db_content_sig(path) != before


def test_upgrade_populated_store_without_counter(store):
    from hermes_state_projection import SESSION_PROJECTION_TRIGGERS

    path, _db = store
    _db.close()
    with sqlite3.connect(path) as writer:
        for name in SESSION_PROJECTION_TRIGGERS:
            writer.execute(f"DROP TRIGGER {name}")
        writer.execute("DELETE FROM state_meta WHERE key='sessions_change_seq'")
    upgraded = SessionDB(path)
    upgraded.close()
    before = server._session_db_content_sig(path)
    assert before[0] == "revision"
    assert before[-1] not in (None, 0)
    with sqlite3.connect(path) as writer:
        assert writer.execute("SELECT id FROM sessions").fetchall() == [('s',)]
        writer.execute("UPDATE sessions SET title='upgraded'")
    assert server._session_db_content_sig(path) != before


def _pre_trigger_store(path, session_id):
    from contextlib import closing

    from hermes_state_projection import SESSION_PROJECTION_TRIGGERS

    db = SessionDB(path)
    db.create_session(session_id, source="cli")
    db.close()
    # Model a populated store written by a release predating the projection triggers.
    with closing(sqlite3.connect(path)) as writer:
        for name in SESSION_PROJECTION_TRIGGERS:
            writer.execute(f"DROP TRIGGER {name}")
        writer.execute("DELETE FROM state_meta WHERE key='sessions_change_seq'")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_divergent_upgraded_stores_restore_in_place_invalidates(tmp_path, monkeypatch):
    from contextlib import closing

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    server._sessions_db_sig_cache.clear()
    server._sessions_digest_at.clear()
    live, backup = tmp_path / "state.db", tmp_path / "backup.db"
    _pre_trigger_store(live, "live-only")
    _pre_trigger_store(backup, "backup-only")
    # Upgrade both; no session writes follow, so only installation can give them a generation.
    SessionDB(live).close()
    SessionDB(backup).close()
    before = server._session_db_content_sig(live)
    assert before[0] == "revision"
    inode = live.stat().st_ino
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(live)) as target:
        source.backup(target)
    assert live.stat().st_ino == inode
    after = server._session_db_content_sig(live)
    assert after != before
    assert after[0] == "revision"
    # A settled reopen of the restored store commits nothing.
    with closing(sqlite3.connect(live)) as observer:
        data_version = observer.execute("PRAGMA data_version").fetchone()
        generation = observer.execute("SELECT value FROM state_meta WHERE key='sessions_change_seq'").fetchone()
        SessionDB(live).close()
        assert observer.execute("PRAGMA data_version").fetchone() == data_version
        assert observer.execute(
            "SELECT value FROM state_meta WHERE key='sessions_change_seq'").fetchone() == generation
    assert server._session_db_content_sig(live) == after


def test_installed_triggers_without_generation_fall_back_to_content(store):
    path, _db = store
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM state_meta WHERE key='sessions_change_seq'")
    fallback = server._session_db_content_sig(path)
    assert fallback[0] == server._SESSION_SIGNATURE_FIELDS


def test_missing_trigger_falls_back_to_content(store):
    path, _db = store
    server._session_db_content_sig(path)
    with sqlite3.connect(path) as writer:
        writer.execute("DROP TRIGGER sessions_projection_update")
    fallback = server._session_db_content_sig(path)
    assert fallback[0] == server._SESSION_SIGNATURE_FIELDS
