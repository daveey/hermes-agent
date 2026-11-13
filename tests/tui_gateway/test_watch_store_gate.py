"""[daveey] Card J (t_01a1d563324d7e8e): the change watcher's store signatures read through one kept connection.

``groups.changed`` and ``sessions.changed`` re-read state.db whenever its DB/WAL mtime moved, and on the hub
state.db commits on most 0.5 s passes: each re-read opened a tracked read-only connection (schema parse), resolved
the store path and closed it (P1: 0.24-0.41% and 0.19-0.22% of a core). A store now keeps one read-only
connection keyed on file identity + ``PRAGMA data_version``; an unchanged store costs that one PRAGMA.
"""
import os
import shutil
import sqlite3
import time

import pytest

import hermes_state
from hermes_state import SessionDB
from tui_gateway import server, watch_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("s", source="cli")
    server._sessions_db_sig_cache.clear()
    server._sessions_digest_at.clear()
    watch_store.reset()
    yield path, db
    watch_store.reset()
    db.close()


def _opens(monkeypatch):
    calls = []
    connect = hermes_state._connect_tracked_db
    monkeypatch.setattr(hermes_state, "_connect_tracked_db", lambda *a, **k: (calls.append(a), connect(*a, **k))[1])
    return calls


def test_a_commit_is_seen_on_the_next_pass_without_reopening(store, monkeypatch):
    path, db = store
    before = server._session_db_content_sig(path)
    opens = _opens(monkeypatch)
    db.create_session("t", source="cli")
    after = server._session_db_content_sig(path)
    assert after != before
    db.set_session_title("t", "renamed")
    assert server._session_db_content_sig(path) != after
    assert opens == [], "the kept connection serves every pass"


def test_a_commit_in_the_same_mtime_tick_is_seen(store, monkeypatch):
    """data_version is exact where a coarse mtime is not: no racy-clean window."""
    path, db = store
    before = server._session_db_content_sig(path)
    stat = os.stat
    frozen = {str(p): stat(p) for p in (path, str(path) + "-wal") if os.path.exists(p)}
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda _paths: 1)
    db.create_session("same-tick", source="cli")
    for p, st in frozen.items():
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert server._session_db_content_sig(path) != before


def test_a_replaced_store_is_reopened(store, tmp_path):
    path, db = store
    before = server._session_db_content_sig(path)
    other = tmp_path / "other.db"
    replacement = SessionDB(other)
    replacement.create_session("other", source="cli")
    replacement.close()
    db.close()
    for suffix in ("-wal", "-shm"):
        with __import__("contextlib").suppress(FileNotFoundError):
            os.unlink(str(path) + suffix)
    shutil.copy(other, str(path) + ".tmp")
    os.replace(str(path) + ".tmp", path)
    assert server._session_db_content_sig(path) != before


def test_a_removed_store_reads_none_and_comes_back(store):
    path, db = store
    assert server._session_db_content_sig(path) is not None
    db.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(str(path) + suffix):
            os.unlink(str(path) + suffix)
    assert server._session_db_content_sig(path) is None
    again = SessionDB(path)
    again.create_session("back", source="cli")
    assert server._session_db_content_sig(path) is not None
    again.close()


def test_legacy_digest_throttle_still_fires_its_trailing_edge_with_no_commit(tmp_path, monkeypatch):
    """The due-time input of this loop: a digest held back by ``SESSION_DIGEST_MIN_S`` is taken once the window
    passes, with no further commit."""
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('a', 'A')")
    server._sessions_db_sig_cache.clear()
    server._sessions_digest_at.clear()
    watch_store.reset()
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server, "SESSION_DIGEST_MIN_S", 3.0)
    before = server._session_db_content_sig(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET title = 'B'")
    clock[0] += 0.5
    assert server._session_db_content_sig(path) == before
    clock[0] += 3.0
    assert server._session_db_content_sig(path) != before
    watch_store.reset()


def test_kept_connections_are_bounded_and_tracked(tmp_path):
    from hermes_cli.sqlite_safe_read import has_live_connection
    watch_store.reset()
    paths = []
    for n in range(watch_store.MAX_STORES + 3):
        p = tmp_path / f"s{n}.db"
        with sqlite3.connect(p) as conn:
            conn.execute("CREATE TABLE t(v)")
        with watch_store.observed(p):
            pass
        paths.append(p)
    assert len(watch_store._held) == watch_store.MAX_STORES
    assert has_live_connection(paths[-1])
    assert not has_live_connection(paths[0]), "the least recently used store was closed"
    watch_store.reset()
    assert not has_live_connection(paths[-1])


def test_a_new_wal_generation_reopens_the_kept_connection(tmp_path):
    """A handle stranded on a replaced -wal/-shm would make the next writer refuse to open: drop it."""
    path = tmp_path / "s.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(v)")
    writer.commit()
    watch_store.reset()
    with watch_store.observed(path) as (first, _conn):
        pass
    wal = str(path) + "-wal"
    assert os.path.exists(wal)
    shutil.copy(wal, wal + ".copy")
    os.replace(wal + ".copy", wal)  # same name, new inode: a new WAL generation
    with watch_store.observed(path) as (second, _conn):
        pass
    assert second[2] != first[2], "reopened (a new generation)"
    with watch_store.observed(path) as (third, _conn):
        pass
    assert third[2] == second[2], "stable once reopened"
    watch_store.reset()
    writer.close()


def test_close_all_under_releases_kept_connections(tmp_path):
    """A profile delete or snapshot restore (``hermes_state_registry.close_all_under``) closes the watcher's kept
    readers under that home first, so rmtree on Windows and the header-probe release see no other descriptor."""
    from hermes_cli.sqlite_safe_read import has_live_connection
    from hermes_state_registry import close_all_under
    watch_store.reset()
    inside, outside = tmp_path / "doomed" / "state.db", tmp_path / "kept" / "state.db"
    for p in (inside, outside):
        p.parent.mkdir()
        with sqlite3.connect(p) as conn:
            conn.execute("CREATE TABLE t(v)")
        with watch_store.observed(p):
            pass
    close_all_under(tmp_path / "doomed")
    assert not has_live_connection(inside)
    assert has_live_connection(outside)
    watch_store.reset()


def test_a_transaction_left_open_drops_the_connection(tmp_path):
    p = tmp_path / "s.db"
    with sqlite3.connect(p) as conn:
        conn.execute("CREATE TABLE t(v)")
    watch_store.reset()
    with watch_store.observed(p) as (_key, conn):
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM t").fetchall()
    assert str(p) not in watch_store._held
    with watch_store.observed(p) as (key, _conn):
        assert key
    watch_store.reset()


# -- replay benchmark ---------------------------------------------------------------------------------------------
# Design §3 card J: serve change_watcher + notification poller 2.0 -> 0.7 (% of one core). The two store reads of
# a watch pass (sessions + groups over the same state.db) are held to 0.15 at 2 passes/s with the hub's stream:
# state.db committed between 92 of 120 sampled passes.

PASSES_PER_S = 2.0
COMMIT_SHARE = 92 / 120
BUDGET_CORE = 0.0015


def test_replay_budget_store_signatures(store):
    path, db = store
    for n in range(200):
        db.create_session(f"bench-{n}", source="cli")
    cache: dict = {}

    def read(conn, _key):
        return tuple(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()), False

    passes = 60
    best = float("inf")
    for _ in range(3):
        spent = 0.0
        for n in range(passes):
            if n / passes < COMMIT_SHARE:
                db.set_session_title(f"bench-{n}", f"t{time.time_ns()}")
            t0 = time.thread_time()
            server._session_db_content_sig(path)
            server._db_content_sig(path, cache, read)
            spent += time.thread_time() - t0
        best = min(best, spent / passes)
    per_s = best * PASSES_PER_S
    assert per_s <= BUDGET_CORE, f"{best * 1000:.3f} ms per pass = {per_s:.3%} of a core"
