"""Released roots outlive observer eviction; transient keys describe content, not connections."""
import contextlib
import os
import shutil
import sqlite3
import time

import pytest

from hermes_cli.sqlite_safe_read import has_live_connection
from hermes_state import _connect_tracked_db
from hermes_state_registry import close_all, close_all_under
from tui_gateway import server, watch_store


@pytest.fixture(autouse=True)
def clean():
    watch_store.reset()
    server._sessions_db_sig_cache.clear()
    server._sessions_digest_at.clear()
    yield
    watch_store.reset()


def create(path, value="A"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('a', ?)", (value,))
        conn.commit()


def observe(path):
    with watch_store.observed(path) as (key, conn):
        assert conn.execute("SELECT title FROM sessions").fetchone()
        return key


@pytest.mark.parametrize("all_roots", [False, True])
def test_release_before_observation_and_reset(tmp_path, all_roots):
    inside = tmp_path / "released" / "state.db"
    outside = tmp_path / "outside" / "state.db"
    create(inside)
    create(outside)
    if all_roots:
        close_all()
    else:
        close_all_under(inside.parent)
    for _ in range(3):
        observe(inside)
        observe(outside)
        assert not has_live_connection(inside)
        assert has_live_connection(outside) is not all_roots
    watch_store.reset()
    observe(inside)
    assert has_live_connection(inside)


def test_release_covers_evicted_stores_and_reactivation_is_per_path(tmp_path):
    paths = [tmp_path / f"s{n}.db" for n in range(watch_store.MAX_STORES + 1)]
    for path in paths:
        create(path)
        observe(path)
    assert not has_live_connection(paths[0])
    close_all_under(tmp_path)
    observe(paths[0])
    assert not has_live_connection(paths[0])
    with contextlib.closing(_connect_tracked_db(paths[0], tracking_path=paths[0])):
        observe(paths[0])
        assert str(paths[0]) in watch_store._held
        observe(paths[1])
        assert not has_live_connection(paths[1])
    close_all_under(tmp_path)
    observe(paths[0])
    assert not has_live_connection(paths[0])


def content(path, cache):
    def read(conn, _key):
        return conn.execute("SELECT title FROM sessions").fetchone()[0], False
    return server._db_content_sig(path, cache, read)


def test_transient_content_stable_and_same_mtime_commit_seen(tmp_path):
    path = tmp_path / "state.db"
    create(path)
    observe(path)
    close_all_under(tmp_path)
    cache = {}
    first = content(path, cache)
    assert content(path, cache) == first
    stamp = path.stat()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE sessions SET title='B'")
        conn.commit()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert content(path, cache) != first
    assert not has_live_connection(path)


def test_transient_restore_equal_mtime(tmp_path):
    path, other = tmp_path / "state.db", tmp_path / "copy.db"
    create(path)
    create(other, "B")
    observe(path)
    close_all_under(tmp_path)
    cache = {}
    before = content(path, cache)
    stamp = path.stat()
    shutil.copyfile(other, path)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert content(path, cache) != before


def test_transient_missing_table_does_not_broadcast_each_throttle(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE unrelated(v)")
    with watch_store.observed(path):
        pass
    close_all_under(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    before = server._session_db_content_sig(path)
    for _ in range(4):
        clock[0] += server.SESSION_DIGEST_MIN_S + 1
        assert server._session_db_content_sig(path) == before
        assert not has_live_connection(path)


def test_transient_due_trailing_edge(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    create(path)
    observe(path)
    close_all_under(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    before = server._session_db_content_sig(path)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE sessions SET title='B'")
        conn.commit()
    clock[0] += 0.5
    assert server._session_db_content_sig(path) == before
    clock[0] += server.SESSION_DIGEST_MIN_S
    assert server._session_db_content_sig(path) != before
    assert not has_live_connection(path)


def test_transient_wal_commit_is_seen_next_pass_without_stat_change(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    create(path)
    observe(path)
    close_all_under(tmp_path)
    cache = {}
    reads = []

    def read(conn, _key):
        reads.append(1)
        return conn.execute("SELECT title FROM sessions").fetchone()[0], False

    # An untracked external writer does not reactivate this process's owner.
    with contextlib.closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        assert server._db_content_sig(path, cache, read) == "A"
        assert server._db_content_sig(path, cache, read) == "A"
        assert len(reads) == 2
        stamp = os.stat
        frozen = {str(p): stamp(p) for p in (path, path.with_name("state.db-wal"))}
        writer.execute("UPDATE sessions SET title='B'")
        writer.commit()
        monkeypatch.setattr(watch_store.os, "stat", lambda p, *a, **k: frozen.get(str(p)) or stamp(p, *a, **k))
        assert server._db_content_sig(path, cache, read) == "B"
        assert len(reads) == 3
        assert not has_live_connection(path)


def test_transient_normal_store_never_serializes(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    path = tmp_path / "state.db"
    with contextlib.closing(SessionDB(path)) as db:
        db.create_session("a", source="cli")
    with watch_store.observed(path) as (_key, conn):
        connection_type = type(conn)
    close_all_under(tmp_path)

    def reject(*_args, **_kwargs):
        pytest.fail("normal sessions must not serialize unrelated database pages")

    monkeypatch.setattr(connection_type, "serialize", reject)
    first = server._session_db_content_sig(path)
    assert first[0] == "revision"
    assert server._session_db_content_sig(path) == first
    with contextlib.closing(sqlite3.connect(path)) as writer:
        writer.execute("UPDATE sessions SET title='new' WHERE id='a'")
        writer.commit()
    assert server._session_db_content_sig(path) != first
    assert not has_live_connection(path)


def test_missing_table_fingerprint_is_throttled_and_snapshot_bound(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE unrelated(v)")
        writer.execute("INSERT INTO unrelated VALUES (1)")
        writer.commit()
        with watch_store.observed(path) as (_key, conn):
            connection_type = type(conn)
        close_all_under(tmp_path)
        clock = [1000.0]
        monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
        first = server._session_db_content_sig(path)
        serialize = connection_type.serialize
        calls = []

        def concurrent_commit(conn, *args, **kwargs):
            assert conn.in_transaction
            calls.append(1)
            if len(calls) == 1:
                stamp = path.with_name("state.db-wal").stat()
                writer.execute("UPDATE unrelated SET v=2")
                writer.commit()
                os.utime(path.with_name("state.db-wal"), ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            return serialize(conn, *args, **kwargs)

        monkeypatch.setattr(connection_type, "serialize", concurrent_commit)
        clock[0] += 0.5
        assert server._session_db_content_sig(path) == first
        assert calls == []
        clock[0] += server.SESSION_DIGEST_MIN_S
        assert server._session_db_content_sig(path) == first, "fingerprint remains in the pre-commit snapshot"
        clock[0] += server.SESSION_DIGEST_MIN_S
        assert server._session_db_content_sig(path) != first
        assert len(calls) == 2
        assert not has_live_connection(path)
    finally:
        writer.close()


def test_transient_replay_cost(tmp_path, record_property):
    path = tmp_path / "state.db"
    create(path)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executemany("INSERT INTO sessions VALUES (?, ?)", [(str(n), "title") for n in range(200)])
        conn.commit()
    observe(path)
    close_all_under(tmp_path)
    before = server._session_db_content_sig(path)
    start = time.thread_time()
    for _ in range(60):
        assert server._session_db_content_sig(path) == before
    elapsed = time.thread_time() - start
    record_property("ms_per_pass", elapsed / 60 * 1000)
    record_property("db_bytes", path.stat().st_size)
    assert not has_live_connection(path)
