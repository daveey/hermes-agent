"""GET /api/sessions single-flight + data-stamp cache (hermes_cli/session_list_flight.py, t_01a19f5af20a19f6).

hermes-hub 2026-11-03: Desktop reconnect bursts of ~15 identical /api/sessions calls each ran the list SQL in
their own threadpool slot and took 2-7 s. Identical concurrent calls must run the SQL once, and any write to
state.db must invalidate the cached result.
"""

import sqlite3
import subprocess
import sys
import threading

import pytest

from tests.waiting import wait_until

from hermes_cli import session_list_flight as slf


def _wal_db(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('a', 'first')")
    conn.commit()
    return path, conn


def test_data_stamp_is_stable_across_reads_and_changes_on_every_write(tmp_path):
    path, conn = _wal_db(tmp_path)
    reader = sqlite3.connect(path)
    before = slf.data_stamp(path)
    assert before is not None
    reader.execute("SELECT * FROM sessions").fetchall()
    assert slf.data_stamp(path) == before
    conn.execute("UPDATE sessions SET title = 'renamed' WHERE id = 'a'")
    conn.commit()
    after_write = slf.data_stamp(path)
    assert after_write != before
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("INSERT INTO sessions VALUES ('b', 'second')")
    conn.commit()
    assert slf.data_stamp(path) not in (before, after_write)


@pytest.mark.platforms("linux", "macos")
def test_data_stamp_keeps_this_process_wal_locks(tmp_path):
    """hermes-hub 2026-11-11: data_stamp() did open()/close() on -shm, and POSIX drops every lock a process
    holds on a file when it closes any descriptor for it. SQLite's WAL writer lock lives on -shm, so a
    GET /api/sessions during a write transaction let another process write concurrently (corrupt indexes).
    A real write transaction is held here; a second process must still be locked out after data_stamp()."""
    path, conn = _wal_db(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE sessions SET title = 'held' WHERE id = 'a'")
    probe = (
        "import sqlite3, sys\n"
        "c = sqlite3.connect(sys.argv[1], timeout=0)\n"
        "try:\n"
        "    c.execute('BEGIN IMMEDIATE')\n"
        "except sqlite3.OperationalError as e:\n"
        "    print('locked' if 'locked' in str(e) else 'error:' + str(e))\n"
        "else:\n"
        "    print('acquired')\n"
    )

    def other_process_writer():
        return subprocess.run([sys.executable, "-I", "-c", probe, str(path)], capture_output=True, text=True,
                              timeout=60, check=True).stdout.strip()

    assert other_process_writer() == "locked"  # the transaction above really holds the writer lock
    for _ in range(3):
        assert slf.data_stamp(path) is not None
    assert other_process_writer() == "locked"
    conn.rollback()


def test_no_wal_means_no_stamp_and_no_caching(tmp_path):
    path = tmp_path / "plain.db"
    sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.commit()
    assert slf.data_stamp(path) is None
    cache = slf.SingleFlightCache()
    calls = []
    for _ in range(3):
        cache.get("k", None, lambda: calls.append(1) or len(calls))
    assert len(calls) == 3


def _session_rows(path):
    conn = sqlite3.connect(path)
    try:
        return {"sessions": [{"id": i, "title": t} for i, t in conn.execute("SELECT id, title FROM sessions ORDER BY id")]}
    finally:
        conn.close()


def test_concurrent_identical_calls_run_the_sql_once_and_a_write_invalidates(tmp_path):
    path, writer = _wal_db(tmp_path)
    cache = slf.SingleFlightCache(ttl_s=60.0, copy=slf.copy_session_list)
    n = 15
    started = threading.Event()
    release = threading.Event()
    sql_runs = []

    def compute():
        sql_runs.append(1)
        started.set()
        release.wait(5)  # hold the leader so every follower arrives while it is in flight
        return _session_rows(path)

    results = [None] * n

    def call(i):
        results[i] = cache.get(("state.db", 100, "recent"), slf.data_stamp(path), compute)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(n)]
    threads[0].start()
    assert started.wait(5)
    for t in threads[1:]:
        t.start()
    # every follower is parked on the leader's flight before it is released
    flight = next(iter(cache._flights.values()))
    wait_until(lambda: flight.followers == n - 1, timeout=5)
    release.set()
    for t in threads:
        t.join(5)
    assert len(sql_runs) == 1
    assert all(r == {"sessions": [{"id": "a", "title": "first"}]} for r in results)
    # each caller owns its copy: one response's edits never leak into another's
    results[0]["sessions"][0]["title"] = "mutated"
    assert results[1]["sessions"][0]["title"] == "first"

    # still fresh and unchanged: a later identical call is a cache hit
    assert cache.get(("state.db", 100, "recent"), slf.data_stamp(path), compute)["sessions"][0]["title"] == "first"
    assert len(sql_runs) == 1

    # a write to state.db invalidates it at once (no TTL wait)
    writer.execute("UPDATE sessions SET title = 'renamed' WHERE id = 'a'")
    writer.commit()
    fresh = cache.get(("state.db", 100, "recent"), slf.data_stamp(path), compute)
    assert len(sql_runs) == 2 and fresh["sessions"][0]["title"] == "renamed"


def test_ttl_expiry_and_different_params_recompute():
    now = [0.0]
    cache = slf.SingleFlightCache(ttl_s=2.0, clock=lambda: now[0])
    runs = []
    get = lambda key: cache.get(key, ("stamp",), lambda: runs.append(key) or key)
    get("a")
    get("a")
    get("b")
    assert runs == ["a", "b"]
    now[0] = 2.5
    get("a")
    assert runs == ["a", "b", "a"]


def test_leader_error_reaches_followers_and_is_not_cached():
    cache = slf.SingleFlightCache(ttl_s=60.0)
    gate = threading.Event()
    in_flight = threading.Event()
    errors = []

    def boom():
        in_flight.set()
        gate.wait(5)
        raise RuntimeError("store busy")

    def call():
        try:
            cache.get("k", ("s",), boom)
        except RuntimeError as exc:
            errors.append(str(exc))

    leader = threading.Thread(target=call)
    leader.start()
    assert in_flight.wait(5)
    follower = threading.Thread(target=call)
    follower.start()
    flight = next(iter(cache._flights.values()))
    wait_until(lambda: flight.followers == 1, timeout=5)
    gate.set()
    leader.join(5)
    follower.join(5)
    assert errors == ["store busy", "store busy"]
    assert cache.computes == 1  # the follower got the leader's error; it did not run boom() itself
    assert cache.get("k", ("s",), lambda: "ok") == "ok"


def test_follower_computes_on_its_own_when_the_leader_hangs(monkeypatch):
    monkeypatch.setattr(slf, "FOLLOW_TIMEOUT_S", 0.2)
    cache = slf.SingleFlightCache(ttl_s=60.0)
    release = threading.Event()
    in_flight = threading.Event()

    def hung():
        in_flight.set()
        release.wait(10)
        return "late"

    leader = threading.Thread(target=lambda: cache.get("k", ("s",), hung))
    leader.start()
    assert in_flight.wait(5)
    try:
        assert cache.get("k", ("s",), lambda: "own") == "own"
        assert cache.computes == 2
    finally:
        release.set()
        leader.join(5)


def test_expired_entries_are_dropped_on_the_next_insert():
    now = [0.0]
    cache = slf.SingleFlightCache(ttl_s=2.0, clock=lambda: now[0])
    cache.get("a", ("s",), lambda: "A")
    now[0] = 3.0
    cache.get("b", ("s",), lambda: "B")
    assert list(cache._done) == ["b"]


def test_get_sessions_route_coalesces_and_sees_writes(tmp_path, monkeypatch):
    """The real handler: two identical GETs on an unchanged store run list_sessions_rich once; a write
    to state.db (a new session) shows up on the very next GET."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB
    from hermes_cli.web_routers import sessions as routes

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="desktop")
    db.append_message("s1", role="user", content="hello there")
    monkeypatch.setattr(routes, "_session_db_path_for_profile", lambda profile: tmp_path / "state.db")

    real_open = routes._open_session_db_for_profile
    monkeypatch.setattr(routes, "_open_session_db_for_profile",
                        lambda profile, read_only: SessionDB(db_path=tmp_path / "state.db", read_only=True)
                        if read_only else real_open(profile, read_only=read_only))
    listings = []
    real_list = SessionDB.list_sessions_rich

    def counting(self, *a, **kw):
        listings.append(1)
        return real_list(self, *a, **kw)

    monkeypatch.setattr(SessionDB, "list_sessions_rich", counting)
    slf.SESSION_LIST_CACHE.clear()
    call = lambda: routes.get_sessions(limit=20, offset=0, min_messages=0, archived="exclude", order="recent",
                                       source=None, sources=None, exclude_sources="cron", cwd_prefix=None,
                                       full=False, profile=None)
    first = call()
    second = call()
    assert [s["id"] for s in first["sessions"]] == ["s1"] == [s["id"] for s in second["sessions"]]
    assert len(listings) == 1

    db.create_session(session_id="s2", source="desktop")
    db.append_message("s2", role="user", content="second chat")
    third = call()
    assert len(listings) == 2
    assert {s["id"] for s in third["sessions"]} == {"s1", "s2"}
    db.close()


@pytest.fixture(autouse=True)
def _clear_cache():
    slf.SESSION_LIST_CACHE.clear()
    yield
    slf.SESSION_LIST_CACHE.clear()
