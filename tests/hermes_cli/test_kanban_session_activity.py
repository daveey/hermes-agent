"""kanban_active stamping on session-list rows (sidebar 'still working' state)."""

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_session_activity as ksa


def _make_board(path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    ksa._reset_cache_for_tests()
    yield tmp_path
    ksa._reset_cache_for_tests()


def test_counts_only_pending_and_running_tasks_across_boards(kanban_root):
    _make_board(kanban_root / "kanban.db", [
        ("t1", "running", "s_parent"),
        ("t2", "todo", "s_parent"),
        ("t3", "done", "s_parent"),
        ("t4", "blocked", "s_blocked"),
        ("t5", "ready", None),
        ("t6", "archived", "s_done"),
    ])
    _make_board(kanban_root / "kanban" / "boards" / "other" / "kanban.db", [
        ("t7", "scheduled", "s_parent"),
        ("t8", "running", "s_other"),
    ])

    assert ksa.active_task_counts() == {"s_parent": 3, "s_other": 1}


def test_annotates_rows_by_whole_compression_lineage(kanban_root):
    _make_board(kanban_root / "kanban.db", [
        ("t1", "running", "s_root"),  # created before compression rotated the id
        ("t2", "done", "s_idle"),
    ])
    rows = [
        {"id": "s_tip", "_lineage_root_id": "s_root", "_lineage_ids": ["s_root", "s_tip"]},
        {"id": "s_idle", "kanban_active": 2},  # stale stamp from a reused dict is cleared
        {"id": "s_none"},
    ]

    ksa.annotate_kanban_activity(rows)

    assert rows[0]["kanban_active"] == 1
    assert "kanban_active" not in rows[1]
    assert "kanban_active" not in rows[2]


def test_missing_or_old_board_never_breaks_the_list(kanban_root):
    old = kanban_root / "kanban.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")  # pre-session_id schema
    conn.commit()
    conn.close()
    rows = [{"id": "s1"}]

    assert ksa.annotate_kanban_activity(rows) == [{"id": "s1"}]


def test_counts_tasks_a_session_subscribed_to_but_did_not_create(kanban_root):
    """A session asked to shepherd someone else's card subscribes to it (tui notify
    sub); it must stay busy until the card lands, not paint "done". [daveey]"""
    path = kanban_root / "kanban.db"
    _make_board(path, [
        ("t1", "running", "s_creator"),
        ("t2", "done", "s_creator"),
        ("t3", "ready", None),
    ])
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT)")
    conn.executemany("INSERT INTO kanban_notify_subs VALUES (?, ?, ?)", [
        ("t1", "tui", "s_creator"),    # creator's own auto-sub: counted once, not twice
        ("t1", "tui", "s_watcher"),
        ("t3", "tui", "s_watcher"),
        ("t2", "tui", "s_late"),       # finished card keeps nobody busy
        ("t1", "telegram", "chat-9"),  # gateway chats are not sidebar sessions
    ])
    conn.commit()
    conn.close()

    assert ksa.active_task_counts() == {"s_creator": 1, "s_watcher": 2}


def test_stamps_blocked_count_separately_from_active(kanban_root):
    """The sidebar shows open and blocked kanban counts after the age. [daveey]"""
    _make_board(kanban_root / "kanban.db", [
        ("t1", "running", "s1"),
        ("t2", "blocked", "s1"),
        ("t3", "blocked", "s1"),
        ("t4", "blocked", "s_only_blocked"),
        ("t5", "done", "s_done"),
    ])
    rows = [{"id": "s1"}, {"id": "s_only_blocked"}, {"id": "s_done", "kanban_blocked": 4}]

    ksa.annotate_kanban_activity(rows)

    assert rows[0]["kanban_active"] == 1 and rows[0]["kanban_blocked"] == 2
    assert "kanban_active" not in rows[1] and rows[1]["kanban_blocked"] == 1
    assert "kanban_blocked" not in rows[2]
    assert ksa.blocked_task_counts() == {"s1": 2, "s_only_blocked": 1}


def _make_full_board(path: Path, tasks: list[tuple], subs: list[tuple[str, str, str]] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, assignee TEXT, "
        "session_id TEXT, created_at INTEGER, started_at INTEGER, current_run_id INTEGER)")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, started_at INTEGER)")
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tasks)
    conn.executemany("INSERT INTO task_runs VALUES (?, ?, ?)", [(t[7], t[0], 500) for t in tasks if t[7]])
    conn.executemany("INSERT INTO kanban_notify_subs VALUES (?, ?, ?)", subs)
    conn.commit()
    conn.close()


def test_session_tasks_lists_created_and_shepherded_cards_the_sidebar_counts(kanban_root):
    _make_full_board(kanban_root / "kanban.db", [
        ("t_run", "Run", "running", "default", "s_me", 100, 200, 7),
        ("t_done", "Done", "done", "default", "s_me", 100, 200, None),
        ("t_other", "Theirs", "blocked", "default", "s_them", 100, None, None),
        ("t_unrel", "Unrelated", "running", "default", "s_them", 100, None, None),
    ], subs=[
        ("t_other", "tui", "s_me"),   # shepherded card created by another chat
        ("t_done", "tui", "s_me"),    # finished: not listed
        ("t_unrel", "slack", "s_me"),  # non-tui subscription: not a chat shepherding it
    ])
    _make_full_board(kanban_root / "kanban" / "boards" / "bb" / "kanban.db", [
        ("t_root", "Before compression", "todo", "bob", "s_root", 100, None, None),
    ])

    got = {t["id"]: t for t in ksa.session_tasks(["s_me", "s_root"])}

    assert set(got) == {"t_run", "t_other", "t_root"}
    assert got["t_run"]["via"] == "created" and got["t_run"]["current_run_started_at"] == 500
    assert got["t_run"]["board"] == "default" and got["t_run"]["title"] == "Run"
    assert got["t_other"]["via"] == "subscribed" and got["t_other"]["status"] == "blocked"
    assert got["t_root"]["board"] == "bb"
    # Same cards the sidebar badge counts (open + blocked).
    ksa._reset_cache_for_tests()
    active, blocked = ksa.active_task_counts(), ksa.blocked_task_counts()
    assert active.get("s_me", 0) + active.get("s_root", 0) == 2
    assert blocked.get("s_me") == 1


def test_session_tasks_empty_ids_and_old_schema(kanban_root):
    _make_board(kanban_root / "kanban.db", [("t1", "running", "s_a")])  # no title/subs columns
    assert ksa.session_tasks([]) == []
    rows = ksa.session_tasks(["s_a"])
    assert [(r["id"], r["title"], r["via"]) for r in rows] == [("t1", None, "created")]


def test_session_kanban_tasks_endpoint(kanban_root, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    _make_full_board(kanban_root / "kanban.db", [
        ("t_run", "Run", "running", "default", "s_me", 100, 200, None),
        ("t_x", "Theirs", "running", "default", "s_them", 100, 200, None),
    ])
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    res = client.get("/api/session-kanban-tasks", params={"ids": "s_me, s_root"})

    assert res.status_code == 200
    assert [t["id"] for t in res.json()["tasks"]] == ["t_run"]
    assert client.get("/api/session-kanban-tasks").status_code == 422


def test_kanban_unseen_counts_subscriptions_trailing_a_terminal_event(kanban_root):
    """[daveey] A finished card whose result no backend claimed shows up as ``kanban_unseen``; a claimed one,
    a silent kind and a running card do not."""
    path = kanban_root / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT)")
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, "
                 "thread_id TEXT DEFAULT '', last_event_id INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO tasks VALUES (?, 'done', NULL)", [("a",), ("b",), ("c",), ("d",)])
    conn.executemany("INSERT INTO task_events (id, task_id, kind) VALUES (?, ?, ?)",
                     [(1, "a", "completed"), (2, "b", "completed"), (3, "c", "archived"), (4, "d", "created")])
    conn.executemany("INSERT INTO kanban_notify_subs VALUES (?, 'tui', ?, '', ?)",
                     [("a", "s_gone", 0), ("b", "s_claimed", 2), ("c", "s_silent", 0), ("d", "s_run", 0)])
    conn.commit()
    conn.close()
    rows = [{"id": "s_gone"}, {"id": "s_claimed"}, {"id": "s_silent"}, {"id": "s_run"}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0]["kanban_unseen"] == 1
    assert all("kanban_unseen" not in r for r in rows[1:])
