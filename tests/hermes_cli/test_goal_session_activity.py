"""goal_active stamping on session-list rows (sidebar 'still working' state)."""

import json
import sqlite3
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


def test_missing_or_schemaless_db_is_harmless(home):
    sqlite3.connect(home / "state.db").close()  # no state_meta table
    rows = [{"id": "s1"}]
    assert gsa.annotate_goal_activity(rows) == [{"id": "s1"}]
