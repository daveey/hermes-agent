"""turn_live stamping on session-list rows: a turn running in any process keeps the row live. [daveey]"""

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
