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
