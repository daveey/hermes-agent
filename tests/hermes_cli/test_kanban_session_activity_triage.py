"""A card block-loop detection escalated to triage still waits on the user, so
its session row paints red; a card created in triage for a specifier does not."""

import sqlite3

import pytest

from hermes_cli import kanban_session_activity as ksa


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    ksa._reset_cache_for_tests()
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT, block_kind TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", [
        ("t_esc", "triage", "s_esc", "needs_input"),     # escalated by block_loop_detected
        ("t_spec", "triage", "s_spec", None),            # born in triage, awaiting a specifier
        ("t_time", "blocked", "s_time", "transient"),    # waits on a retry, not a human
        ("t_human", "blocked", "s_human", "capability"),
    ])
    conn.commit()
    conn.close()
    yield tmp_path
    ksa._reset_cache_for_tests()


def test_escalated_triage_card_counts_as_waiting_on_the_user(kanban_root):
    assert ksa.blocked_task_counts() == {"s_esc": 1, "s_human": 1}


def test_escalated_triage_card_is_listed_on_its_session(kanban_root):
    assert [t["id"] for t in ksa.session_tasks(["s_esc"])] == ["t_esc"]
