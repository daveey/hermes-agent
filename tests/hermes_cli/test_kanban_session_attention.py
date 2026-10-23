"""kanban_attention stamping: done cards a registered check flags paint the row amber, not green. [daveey]"""

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_session_activity as ksa


def _make_board(path: Path, rows, runs=(), subs=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, session_id TEXT,
                            completed_at INTEGER, result TEXT);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, summary TEXT);
        CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, last_event_id INTEGER DEFAULT 0);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT);
    """)
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO task_runs (task_id, summary) VALUES (?, ?)", runs)
    conn.executemany("INSERT INTO kanban_notify_subs (task_id, platform, chat_id) VALUES (?, 'tui', ?)", subs)
    conn.commit()
    conn.close()


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    ksa._reset_cache_for_tests()
    yield tmp_path
    ksa._reset_cache_for_tests()


def _suspect(card):
    return "split to follow-up" in (card.get("summary") or "") + (card.get("title") or "")


def test_no_check_registered_means_no_field(kanban_root):
    now = int(time.time())
    _make_board(kanban_root / "kanban.db", [("t1", "x (split to follow-up)", "done", "s1", now, None)])
    rows = [{"id": "s1"}]
    ksa.annotate_kanban_activity(rows)
    assert "kanban_attention" not in rows[0]


def test_flagged_done_card_stamps_attention(kanban_root):
    now = int(time.time())
    _make_board(kanban_root / "kanban.db", [
        ("t1", "Mac pod (iMac run split to follow-up)", "done", "s_flag", now - 60, None),
        ("t2", "clean", "done", "s_clean", now - 60, None),
        ("t3", "old", "done", "s_old", now - 2 * 86400, None),  # outside the 24 h window
        ("t4", "open", "running", "s_open", None, None),
        ("t5", "sub", "done", "s_owner", now - 60, None),
    ], runs=[("t1", "shipped"), ("t2", "shipped, verified"), ("t3", "split to follow-up"),
             ("t5", "split to follow-up")], subs=[("t5", "s_shepherd")])
    _make_board(kanban_root / "kanban" / "boards" / "b2" / "kanban.db", [
        ("t6", "y", "done", "s_flag", now - 10, "split to follow-up"),  # result fallback, other board
    ])
    seen = []
    dispose = ksa.register_done_check("test", lambda c: seen.append(c) or _suspect(c))
    try:
        rows = [{"id": "s_flag"}, {"id": "s_clean"}, {"id": "s_old"}, {"id": "s_open"}, {"id": "s_owner"},
                {"id": "s_tip", "_lineage_ids": ["s_shepherd", "s_tip"]}, {"id": "s_x", "kanban_attention": 4}]
        ksa.annotate_kanban_activity(rows)
    finally:
        dispose()
    by = {r["id"]: r.get("kanban_attention") for r in rows}
    assert by == {"s_flag": 2, "s_clean": None, "s_old": None, "s_open": None, "s_owner": 1, "s_tip": 1, "s_x": None}
    card = next(c for c in seen if c["id"] == "t1")
    assert card["summary"] == "shipped" and card["board"] == "default" and card["board_path"].endswith("kanban.db")
    assert next(c for c in seen if c["id"] == "t6")["board"] == "b2"


def test_raising_check_counts_as_clean_and_dispose_unregisters(kanban_root):
    now = int(time.time())
    _make_board(kanban_root / "kanban.db", [("t1", "x", "done", "s1", now, "split to follow-up")])

    def boom(card):
        raise RuntimeError("bad plugin")

    dispose = ksa.register_done_check("boom", boom)
    rows = [{"id": "s1"}]
    ksa.annotate_kanban_activity(rows)
    assert "kanban_attention" not in rows[0]
    dispose()
    ksa.register_done_check("ok", _suspect)
    try:
        ksa.annotate_kanban_activity(rows)
        assert rows[0]["kanban_attention"] == 1
    finally:
        ksa._done_checks.clear()
        ksa._reset_cache_for_tests()
