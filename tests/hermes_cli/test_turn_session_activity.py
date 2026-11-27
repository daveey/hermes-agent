"""turn_live stamping on session-list rows: a turn running in any process keeps the row live. [daveey]"""

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_cli import turn_session_activity as tsa


def _leases(db_path, rows):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_turn_leases (conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL,"
        " acquired_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.executemany("INSERT INTO session_turn_leases VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tsa._reset_cache_for_tests()
    yield tmp_path
    tsa._reset_cache_for_tests()


@pytest.fixture
def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _holder(pid, sid):
    return f"pid={pid}:turn={sid}:{sid}:abcd1234:platform=desktop"


def test_live_lease_stamps_row_and_its_lineage_tip(home):
    now = time.time()
    _leases(home / "state.db", [("root_a", _holder(os.getpid(), "root_a"), now - 5, now + 600)])
    rows = tsa.annotate_turn_activity([
        {"id": "root_a"},
        # A compression tip carries its root: the lease is keyed by the root.
        {"id": "tip_a", "_lineage_root_id": "root_a"},
        {"id": "other"},
    ])
    assert [r.get("turn_live") for r in rows] == [True, True, None]


def test_no_lease_no_stamp_and_stale_stamp_is_removed(home):
    _leases(home / "state.db", [])
    rows = tsa.annotate_turn_activity([{"id": "a", "turn_live": True}])
    assert "turn_live" not in rows[0]


def test_expired_lease_does_not_count(home):
    now = time.time()
    _leases(home / "state.db", [("a", _holder(os.getpid(), "a"), now - 900, now - 1)])
    assert "turn_live" not in tsa.annotate_turn_activity([{"id": "a"}])[0]


def test_lease_of_dead_holder_does_not_count(home, dead_pid):
    now = time.time()
    _leases(home / "state.db", [("a", _holder(dead_pid, "a"), now - 5, now + 600)])
    assert "turn_live" not in tsa.annotate_turn_activity([{"id": "a"}])[0]


def test_lease_held_by_another_live_process_counts(home):
    # The SSH-isolated serve / dashboard / gateway hold turns the listing backend never ran.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        now = time.time()
        _leases(home / "state.db", [("a", _holder(proc.pid, "a"), now - 5, now + 600)])
        assert tsa.annotate_turn_activity([{"id": "a"}])[0].get("turn_live") is True
    finally:
        proc.kill()
        proc.wait()


def test_profile_state_dbs_are_read(home):
    now = time.time()
    _leases(home / "state.db", [])
    _leases(home / "profiles" / "work" / "state.db", [("p", _holder(os.getpid(), "p"), now - 5, now + 600)])
    assert tsa.annotate_turn_activity([{"id": "p", "profile": "work"}])[0].get("turn_live") is True


def test_missing_lease_table_and_missing_db_never_raise(home):
    sqlite3.connect(home / "state.db").close()  # a store with no lease table yet
    assert tsa.annotate_turn_activity([{"id": "a"}]) == [{"id": "a"}]
    assert tsa.annotate_turn_activity([]) == []


# ── list endpoints: the rows the sidebar paints carry the stamp ─────────────────────────────────


@pytest.fixture
def rest(monkeypatch, _isolate_hermes_home):
    starlette = pytest.importorskip("starlette.testclient")
    import hermes_state
    from hermes_cli import profiles
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: home / "profiles")
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    db = SessionDB(db_path=home / "state.db")
    try:
        for sid in ("busy_chat", "idle_chat"):
            db.create_session(sid, source="desktop")
            db.append_message(sid, role="user", content="go")
            db.append_message(sid, role="assistant", content="done")
        # The close-out turn another backend process runs in busy_chat.
        assert db.try_acquire_session_turn_lease("busy_chat", _holder(os.getpid(), "busy_chat"), ttl_seconds=600)
    finally:
        db.close()
    tsa._reset_cache_for_tests()
    c = starlette.TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    yield c
    tsa._reset_cache_for_tests()


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_api_sessions_rows_carry_turn_live(rest):
    rows = _by_id(rest.get("/api/sessions?order=recent&limit=20").json()["sessions"])
    assert rows["busy_chat"].get("turn_live") is True
    assert "turn_live" not in rows["idle_chat"]


def test_api_profiles_sessions_rows_carry_turn_live(rest):
    rows = _by_id(rest.get("/api/profiles/sessions?profile=all&limit=20").json()["sessions"])
    assert rows["busy_chat"].get("turn_live") is True
    assert "turn_live" not in rows["idle_chat"]


def test_api_profiles_sidebar_rows_carry_turn_live(rest):
    body = rest.get("/api/profiles/sessions/sidebar?profile=all").json()
    rows = _by_id(body["recents"]["sessions"])
    assert rows["busy_chat"].get("turn_live") is True
    assert "turn_live" not in rows["idle_chat"]


# ── a turn a core update paused keeps the row live (t_01a1d6354e5847dd) ─────────────────────────


def _checkpointed_db(home, sid, *, status="started", message_age=30.0, receipt_ok=True):
    """A real state.db with one admission for ``sid`` parked on a gateway update checkpoint."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(sid, source="desktop")
        db.append_message(sid, role="user", content="go")
        db.append_message(sid, role="assistant", content="working")
    finally:
        db.close()
    conn = sqlite3.connect(home / "state.db")
    mid = conn.execute("SELECT max(id) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
    conn.execute("UPDATE messages SET timestamp=? WHERE id=?", (time.time() - message_age, mid))
    conn.execute(
        "INSERT INTO session_admissions(admission_id, request_id, principal_id, target_session_id, lineage_json,"
        " payload_json, payload_digest, intent, status, owner_epoch, generation)"
        " VALUES ('adm1', 'r1', 'p', ?, '[]', '{}', 'd', 'queue', ?, 1, 1)", (sid, status))
    receipt = {"max_message_id": mid} if receipt_ok else {"other": 1}
    conn.execute("INSERT INTO state_meta(key, value) VALUES (?, ?)",
                 ("gateway.update_checkpoint.v1.adm1", json.dumps(receipt)))
    conn.commit()
    conn.close()


def test_gateway_update_paused_turn_counts_as_live(home):
    _checkpointed_db(home, "paused_chat")
    rows = tsa.annotate_turn_activity([{"id": "paused_chat"}, {"id": "tip", "_lineage_root_id": "paused_chat"},
                                       {"id": "other"}])
    assert [r.get("turn_live") for r in rows] == [True, True, None]


@pytest.mark.parametrize("kwargs", [
    {"message_age": 2 * 3600.0},   # nobody will auto-continue a pause this old
    {"status": "terminal"},        # settled: the checkpoint's admission is no longer the started head
    {"receipt_ok": False},         # unreadable receipt: no proof of the pause
])
def test_gateway_checkpoint_that_cannot_continue_does_not_count(home, kwargs):
    _checkpointed_db(home, "paused_chat", **kwargs)
    assert "turn_live" not in tsa.annotate_turn_activity([{"id": "paused_chat"}])[0]


def test_gateway_checkpoint_read_never_scans_the_admissions_table(home):
    """The poll runs every 2 s in every profile store: it must reach admissions by index, whatever their
    count, and still find the one checkpointed started admission among settled ones."""
    _checkpointed_db(home, "paused_chat")
    conn = sqlite3.connect(home / "state.db")
    conn.executemany(
        "INSERT INTO session_admissions(admission_id, request_id, principal_id, target_session_id, lineage_json,"
        " payload_json, payload_digest, intent, status) VALUES (?, ?, 'p', 'paused_chat', '[]', '{}', 'd', 'queue',"
        " 'terminal')", [(f"old{i}", f"old-r{i}") for i in range(200)])
    conn.execute("INSERT INTO state_meta(key, value) VALUES ('gateway.update_checkpoint.v1.old7', '{}')")
    conn.execute("INSERT INTO state_meta(key, value) VALUES ('gateway.update_checkpoint.v2.adm1', '{}')")
    conn.commit()
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        assert tsa._update_paused_gateway_turns(conn, time.time()) == {"paused_chat"}
    finally:
        conn.set_trace_callback(None)
    reads = [sql for sql in seen if "session_admissions" in sql]
    assert reads
    for sql in reads:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert plan and all(step.startswith("SEARCH") for step in plan), plan
    conn.close()


def _marker(home, key, **entry):
    (home / "desktop").mkdir(parents=True, exist_ok=True)
    (home / "desktop" / "interrupted_turns.json").write_text(json.dumps({key: entry}), encoding="utf-8")


def test_tui_update_pause_marker_counts_as_live(home):
    now = time.time()
    _leases(home / "state.db", [])
    _marker(home, "tui_chat", reason="core_update_pause", auto_continue=True, attempts=0,
            started_at=now - 7200, paused_at=now - 60)  # a long turn paused a minute ago is fresh
    assert tsa.annotate_turn_activity([{"id": "tui_chat"}])[0].get("turn_live") is True


@pytest.mark.parametrize("entry", [
    {"reason": "core_update_pause", "auto_continue": True, "attempts": 0, "paused_at": -3600},  # stale
    {"reason": "core_update_pause", "auto_continue": False, "attempts": 0, "paused_at": -60},
    {"reason": "core_update_pause", "auto_continue": True, "attempts": 2, "paused_at": -60},  # exhausted
    {"reason": "backend_exit", "auto_continue": True, "attempts": 0, "paused_at": -60},  # crash marker
])
def test_tui_marker_that_is_not_a_live_pause_does_not_count(home, entry):
    now = time.time()
    _leases(home / "state.db", [])
    _marker(home, "tui_chat", **{**entry, "started_at": now - 7200, "paused_at": now + entry["paused_at"]})
    assert "turn_live" not in tsa.annotate_turn_activity([{"id": "tui_chat"}])[0]


def test_profile_tui_marker_is_read(home):
    now = time.time()
    _leases(home / "state.db", [])
    _leases(home / "profiles" / "work" / "state.db", [])
    _marker(home / "profiles" / "work", "w_chat", reason="core_update_pause", auto_continue=True, attempts=0,
            started_at=now - 30)
    assert tsa.annotate_turn_activity([{"id": "w_chat"}])[0].get("turn_live") is True


def _auto_continue_config(home, **auto_continue):
    lines = "".join(f"    {k}: {json.dumps(v)}\n" for k, v in auto_continue.items())
    (home / "config.yaml").write_text(f"desktop:\n  auto_continue:\n{lines}", encoding="utf-8")


@pytest.mark.parametrize(("config", "age_s", "attempts"), [
    ({"enabled": False}, 60, 0),           # auto-continue off: the producer never continues it
    ({"freshness_minutes": 5}, 600, 0),    # a 10-min-old pause is past a 5-min window
    ({"max_attempts": 1}, 60, 1),          # attempts exhausted under a lowered cap
])
def test_tui_pause_outside_configured_auto_continue_bounds_does_not_count(home, config, age_s, attempts):
    now = time.time()
    _leases(home / "state.db", [])
    _auto_continue_config(home, **config)
    _marker(home, "tui_chat", reason="core_update_pause", auto_continue=True, attempts=attempts,
            started_at=now - 7200, paused_at=now - age_s)
    assert "turn_live" not in tsa.annotate_turn_activity([{"id": "tui_chat"}])[0]


def test_tui_pause_within_a_raised_configured_window_counts(home):
    now = time.time()
    _leases(home / "state.db", [])
    _auto_continue_config(home, freshness_minutes=60, max_attempts=3)
    _marker(home, "tui_chat", reason="core_update_pause", auto_continue=True, attempts=2,
            started_at=now - 7200, paused_at=now - 1800)
    assert tsa.annotate_turn_activity([{"id": "tui_chat"}])[0].get("turn_live") is True


def test_profile_tui_pause_uses_that_profiles_auto_continue_config(home):
    now = time.time()
    work = home / "profiles" / "work"
    _leases(home / "state.db", [])
    _leases(work / "state.db", [])
    _auto_continue_config(work, enabled=False)  # the root home keeps the defaults (enabled)
    for h, key in ((home, "root_chat"), (work, "w_chat")):
        _marker(h, key, reason="core_update_pause", auto_continue=True, attempts=0, started_at=now - 30)
    rows = tsa.annotate_turn_activity([{"id": "root_chat"}, {"id": "w_chat"}])
    assert [r.get("turn_live") for r in rows] == [True, None]


def test_producer_and_reader_parse_auto_continue_bounds_alike():
    from tui_gateway.turn_marker import auto_continue_bounds
    assert auto_continue_bounds({}) == (True, 900.0, 2)
    assert auto_continue_bounds({"desktop": {"auto_continue": {
        "enabled": "off", "freshness_minutes": "5", "max_attempts": -1}}}) == (False, 300.0, 2)
    assert auto_continue_bounds({"desktop": {"auto_continue": {"freshness_minutes": "x", "max_attempts": 0}}}) \
        == (True, 900.0, 0)
    # Unparseable max_attempts falls back to the default; a negative window floors at 0 (never negative).
    assert auto_continue_bounds({"desktop": {"auto_continue": {"max_attempts": "abc"}}}) == (True, 900.0, 2)
    assert auto_continue_bounds({"desktop": {"auto_continue": {"freshness_minutes": -5}}}) == (True, 0.0, 2)


_PINNED_NOW = 1_800_000_000.0  # integral, so now - (now - 900.0) is exactly 900.0


def test_tui_pause_exactly_at_the_freshness_window_counts(home):
    """The producer continues a pause whose age equals the window (it drops only ``age > window``), so the
    reader must count it too, or the row reads idle while its turn is about to resume."""
    _marker(home, "tui_chat", reason="core_update_pause", auto_continue=True, attempts=0,
            started_at=_PINNED_NOW - 7200, paused_at=_PINNED_NOW - 900.0)
    assert tsa._update_paused_tui_turns(home, _PINNED_NOW) == {"tui_chat"}
    assert tsa._update_paused_tui_turns(home, _PINNED_NOW + 0.5) == set()  # just past it: stale


def test_tui_pause_with_negative_freshness_floors_the_window_at_zero(home):
    """``freshness_minutes: -5`` floors at a zero window: a pause of age 0 still counts (as the producer
    continues it); without the floor the window is -300 s and nothing ever counts."""
    _auto_continue_config(home, freshness_minutes=-5)
    _marker(home, "tui_chat", reason="core_update_pause", auto_continue=True, attempts=0,
            started_at=_PINNED_NOW - 7200, paused_at=_PINNED_NOW)
    assert tsa._update_paused_tui_turns(home, _PINNED_NOW) == {"tui_chat"}
    assert tsa._update_paused_tui_turns(home, _PINNED_NOW + 1) == set()


def test_tui_pause_with_unparseable_max_attempts_uses_the_default_cap(home):
    """``max_attempts: "abc"`` parses as the default cap (2), as the producer parses it: attempt 1 counts,
    attempt 2 is exhausted. The reader must not raise on it."""
    _auto_continue_config(home, max_attempts="abc")
    for attempts, expected in ((1, {"tui_chat"}), (2, set())):
        _marker(home, "tui_chat", reason="core_update_pause", auto_continue=True, attempts=attempts,
                started_at=_PINNED_NOW - 7200, paused_at=_PINNED_NOW - 60)
        assert tsa._update_paused_tui_turns(home, _PINNED_NOW) == expected


def test_garbage_home_config_falls_open_to_default_bounds(home):
    """A torn/invalid home config.yaml reads as the defaults (enabled, 15 min, 2 attempts), never raises."""
    (home / "config.yaml").write_bytes(b"\xff\xfe desktop: [unclosed\n  - : :")
    assert tsa._auto_continue_bounds(home) == (True, 900.0, 2)


def test_unreadable_home_config_falls_open_to_default_bounds(home, monkeypatch):
    """When loading the home's config raises (unreadable file, loader bug), the reader falls open to the
    defaults like the producer's ``_load_cfg``, so a fresh pause still counts instead of the sweep raising."""
    from hermes_cli import config_effective

    def _unreadable(path, **_kw):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(config_effective, "load_profile_config_effective", _unreadable)
    assert tsa._auto_continue_bounds(home) == (True, 900.0, 2)
    _marker(home, "tui_chat", reason="core_update_pause", auto_continue=True, attempts=0,
            started_at=_PINNED_NOW - 7200, paused_at=_PINNED_NOW - 60)
    assert tsa._update_paused_tui_turns(home, _PINNED_NOW) == {"tui_chat"}


# [daveey] t_01a21ab4eb964866: an idle store is read once per commit, not once per collect; every clock-dependent
# judgement (lease expiry, holder liveness, pause freshness) is still made per collect, and a commit is never lost.

@pytest.fixture
def wal_store(home):
    path = home / "state.db"
    _leases(path, [])
    keeper = sqlite3.connect(path)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("SELECT * FROM session_turn_leases").fetchall()  # initialize the WAL index
    yield path, keeper
    keeper.close()


def _count_opens(monkeypatch):
    opened = []
    original = tsa._open_ro

    def counting(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(tsa, "_open_ro", counting)
    return opened


def _lease(keeper, sid, holder_pid, acquired_at, expires_at):
    keeper.execute("INSERT INTO session_turn_leases VALUES (?, ?, ?, ?)",
                   (sid, _holder(holder_pid, sid), acquired_at, expires_at))
    keeper.commit()


def test_unchanged_store_is_not_reopened_on_every_collect(wal_store, monkeypatch):
    path, keeper = wal_store
    now = time.time()
    _lease(keeper, "a", os.getpid(), now - 5, now + 600)
    opened = _count_opens(monkeypatch)
    assert tsa._collect(now) == {"a"}
    assert tsa._collect(now + 1) == {"a"}
    assert tsa._collect(now + 2) == {"a"}
    assert opened == [path]  # one read for one commit; the 2 s answer TTL is not what saved it


def test_commit_after_a_retained_read_is_seen_by_the_next_collect(wal_store):
    _path, keeper = wal_store
    now = time.time()
    assert tsa._collect(now) == frozenset()
    _lease(keeper, "late", os.getpid(), now, now + 600)
    assert tsa._collect(now + 0.001) == {"late"}  # no TTL: the commit moves the stamp
    keeper.execute("DELETE FROM session_turn_leases WHERE conversation_id = 'late'")
    keeper.commit()
    assert tsa._collect(now + 0.002) == frozenset()


def test_retained_lease_expires_by_the_collect_clock_without_a_write(wal_store, monkeypatch):
    _path, keeper = wal_store
    now = time.time()
    _lease(keeper, "a", os.getpid(), now - 5, now + 10)
    assert tsa._collect(now) == {"a"}
    opened = _count_opens(monkeypatch)
    assert tsa._collect(now + 11) == frozenset()
    assert opened == []  # judged on the retained rows


def test_retained_lease_of_a_holder_that_died_stops_counting(wal_store):
    _path, keeper = wal_store
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        now = time.time()
        _lease(keeper, "a", proc.pid, now - 5, now + 600)
        assert tsa._collect(now) == {"a"}
    finally:
        proc.kill()
        proc.wait()
    assert tsa._collect(now + 1) == frozenset()


def test_retained_gateway_pause_leaves_the_freshness_window_by_the_collect_clock(wal_store):
    _path, keeper = wal_store
    now = time.time()
    keeper.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    keeper.execute("CREATE TABLE session_admissions (admission_id TEXT PRIMARY KEY, target_session_id TEXT, "
                   "status TEXT)")
    keeper.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, timestamp REAL)")
    keeper.execute("INSERT INTO messages VALUES (7, ?)", (now - 10,))
    keeper.execute("INSERT INTO session_admissions VALUES ('adm', 'paused_chat', 'started')")
    keeper.execute("INSERT INTO state_meta VALUES (?, ?)",
                   (tsa._GATEWAY_CHECKPOINT_PREFIX + "adm", json.dumps({"max_message_id": 7})))
    keeper.commit()
    assert tsa._collect(now) == {"paused_chat"}
    assert tsa._collect(now + tsa._GATEWAY_PAUSE_FRESH_S) == frozenset()


def test_failed_store_read_is_not_retained(wal_store, monkeypatch):
    _path, keeper = wal_store
    now = time.time()
    _lease(keeper, "a", os.getpid(), now - 5, now + 600)
    original = tsa._open_ro

    def failed(_path):
        raise sqlite3.OperationalError("busy")

    monkeypatch.setattr(tsa, "_open_ro", failed)
    assert tsa._collect(now) == frozenset()
    monkeypatch.setattr(tsa, "_open_ro", original)
    assert tsa._collect(now) == {"a"}


def test_removed_profile_store_drops_its_retained_read(home, wal_store):
    now = time.time()
    work = home / "profiles" / "work" / "state.db"
    _leases(work, [("p", _holder(os.getpid(), "p"), now - 5, now + 600)])
    keeper = sqlite3.connect(work)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("SELECT * FROM session_turn_leases").fetchall()  # initialize the WAL index
    try:
        assert "p" in tsa._collect(now)
        assert work in tsa._store_reads, "a WAL store with a known stamp is retained"
    finally:
        keeper.close()
    for suffix in ("", "-wal", "-shm"):
        (work.parent / f"state.db{suffix}").unlink(missing_ok=True)
    assert "p" not in tsa._collect(now + 1)
    assert work not in tsa._store_reads
