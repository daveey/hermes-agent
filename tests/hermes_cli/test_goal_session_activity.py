"""goal_active stamping on session-list rows (sidebar 'still working' state)."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import goal_session_activity as gsa


def _make_state_db(path: Path, goals: dict[str, dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO state_meta VALUES (?, ?)",
        [(f"goal:{sid}", v if isinstance(v, str) else json.dumps(v)) for sid, v in goals.items()]
        + [("generation", "x"), ("goalpost", "{}")],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gsa._reset_cache_for_tests()
    yield tmp_path
    gsa._reset_cache_for_tests()


def test_only_active_goals_count_across_profiles(home):
    _make_state_db(home / "state.db", {
        "s_active": {"goal": "climb the ladder", "status": "active", "last_verdict": "wait"},
        "s_paused": {"goal": "x", "status": "paused"},
        "s_done": {"goal": "x", "status": "done"},
        "s_blank": {"goal": "", "status": "active"},
        "s_junk": "not json",
    })
    _make_state_db(home / "profiles" / "work" / "state.db", {
        "s_prof": {"goal": "ship it", "status": "active"},
    })

    assert gsa.active_goal_sessions() == {"s_active", "s_prof"}


def test_annotates_rows_by_lineage_and_clears_stale_stamps(home):
    _make_state_db(home / "state.db", {"s_root": {"goal": "g", "status": "active"}})
    rows = [
        {"id": "s_tip", "_lineage_root_id": "s_root", "_lineage_ids": ["s_root", "s_tip"]},
        {"id": "s_idle", "goal_active": True},  # finished goal: stale stamp is cleared
        {"id": "s_none"},
    ]

    gsa.annotate_goal_activity(rows)

    assert rows[0]["goal_active"] is True
    assert "goal_active" not in rows[1]
    assert "goal_active" not in rows[2]


def test_committed_goal_change_is_visible_without_ttl(home):
    path = home / "state.db"
    _make_state_db(path, {"s": {"goal": "g", "status": "active"}})
    assert gsa.active_goal_sessions() == {"s"}
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE state_meta SET value=? WHERE key='goal:s'",
                     (json.dumps({"goal": "g", "status": "done"}),))
    assert gsa.active_goal_sessions() == set()


def test_unchanged_requests_do_not_repeat_sql_or_profile_glob(home, monkeypatch):
    _make_state_db(home / "state.db", {"s": {"goal": "g", "status": "active"}})
    _make_state_db(home / "profiles" / "work" / "state.db", {})
    assert gsa.active_goal_sessions() == {"s"}
    monkeypatch.setattr(gsa, "_read_active", lambda _: pytest.fail("unchanged SQL read"))
    monkeypatch.setattr(Path, "glob", lambda *a: pytest.fail("unchanged profile glob"))
    monkeypatch.setattr(Path, "iterdir", lambda *a: pytest.fail("unchanged directory scan"))
    # Advancing time must not force a reread; the database change is the signal.
    monkeypatch.setattr(time, "monotonic", lambda: 1e12)
    assert gsa.active_goal_sessions() == {"s"}


def test_profile_add_remove_and_database_created_in_existing_profile(home):
    profile = home / "profiles" / "work"
    profile.mkdir(parents=True)
    assert not gsa.active_goal_sessions()
    path = profile / "state.db"
    _make_state_db(path, {"s": {"goal": "g", "status": "active"}})
    assert gsa.active_goal_sessions() == {"s"}
    path.unlink()
    assert not gsa.active_goal_sessions()
    _make_state_db(home / "profiles" / "new" / "state.db",
                   {"new": {"goal": "g", "status": "active"}})
    assert gsa.active_goal_sessions() == {"new"}


def test_error_is_not_cached(home, monkeypatch):
    path = home / "state.db"
    _make_state_db(path, {"s": {"goal": "g", "status": "active"}})
    original = gsa._read_active
    def failed(_):
        raise sqlite3.OperationalError("busy")
    monkeypatch.setattr(gsa, "_read_active", failed)
    assert not gsa.active_goal_sessions()
    monkeypatch.setattr(gsa, "_read_active", original)
    assert gsa.active_goal_sessions() == {"s"}


def test_wal_commit_and_replacement(home):
    path = home / "state.db"
    _make_state_db(path, {"s": {"goal": "g", "status": "active"}})
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        assert gsa.active_goal_sessions() == {"s"}
        conn.execute("DELETE FROM state_meta")
        conn.commit()
        assert not gsa.active_goal_sessions()
    finally:
        conn.close()
    replacement = home / "replacement.db"
    _make_state_db(replacement, {"other": {"goal": "g", "status": "active"}})
    replacement.replace(path)
    assert gsa.active_goal_sessions() == {"other"}


def test_write_during_read_is_not_certified(home, monkeypatch):
    path = home / "state.db"
    _make_state_db(path, {"s": {"goal": "g", "status": "active"}})
    original = gsa._read_active
    def raced(p):
        result = original(p)
        conn = sqlite3.connect(p)
        try:
            conn.execute("DELETE FROM state_meta")
            conn.commit()
        finally:
            conn.close()
        return result
    monkeypatch.setattr(gsa, "_read_active", raced)
    assert gsa.active_goal_sessions() == {"s"}
    assert path not in gsa._cache
    monkeypatch.setattr(gsa, "_read_active", original)
    assert not gsa.active_goal_sessions()


def test_reads_close_connections(home, monkeypatch):
    _make_state_db(home / "state.db", {})
    opened = []
    original = sqlite3.connect
    def connect(*a, **kw):
        conn = original(*a, **kw)
        opened.append(conn)
        return conn
    monkeypatch.setattr(gsa.sqlite3, "connect", connect)
    assert not gsa.active_goal_sessions()
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_missing_or_schemaless_db_is_harmless(home):
    sqlite3.connect(home / "state.db").close()  # no state_meta table
    rows = [{"id": "s1"}]
    assert gsa.annotate_goal_activity(rows) == [{"id": "s1"}]
