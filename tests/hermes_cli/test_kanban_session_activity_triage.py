"""A card block-loop detection escalated to triage still waits on the user, so
its session row paints red; a card created in triage for a specifier does not."""

import sqlite3

import pytest

from hermes_cli import kanban_session_activity as ksa


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
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


@pytest.mark.parametrize("kind,bucket", [
    ("transient", "active"), (None, "active"),
    ("needs_input", "blocked"), ("capability", "blocked"),
])
def test_triage_is_unfinished_and_visible_for_owner_and_subscriber(tmp_path, monkeypatch, kind, bucket):
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, session_id TEXT, block_kind TEXT)")
        conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('card', 'triage', 'ancestor', ?)", (kind,))
        conn.execute("INSERT INTO kanban_notify_subs VALUES ('card', 'tui', 'watcher')")
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [path])
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    rows = [{"id": "leaf", "_lineage_ids": ["ancestor", "leaf"]}, {"id": "watcher"}]
    try:
        ksa.annotate_kanban_activity(rows)
        for row in rows:
            assert row.get("kanban_active", 0) == (1 if bucket == "active" else 0)
            assert row.get("kanban_blocked", 0) == (1 if bucket == "blocked" else 0)
    finally:
        ksa._reset_cache_for_tests()
    expected = {"ancestor": 1, "watcher": 1}
    assert ksa._read_counts(path) == (expected if bucket == "active" else {})
    assert ksa._read_counts(path, ksa.BLOCKED_STATUSES) == (expected if bucket == "blocked" else {})
    assert ksa._replica_bucket({"status": "triage", "block_kind": kind}) == bucket
    assert [t["id"] for t in ksa._read_session_tasks(path, ["leaf", "ancestor"], ksa.LISTED_STATUSES)] == ["card"]
    assert [t["id"] for t in ksa._read_session_tasks(path, ["watcher"], ksa.LISTED_STATUSES)] == ["card"]


def test_legacy_triage_remains_open_without_block_kind(tmp_path):
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, session_id TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('card', 'triage', 'owner')")
    assert ksa._read_counts(path) == {"owner": 1}
    assert ksa._read_counts(path, ksa.BLOCKED_STATUSES) == {}
