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
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))  # never the real hermes-hub replica
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


def test_corrupt_local_board_is_unknown_not_successful_empty(kanban_root):
    (kanban_root / "kanban.db").write_bytes(b"not a database" * 100)
    rows = [{"id": "s1"}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0].get("kanban_unknown") is True
    with pytest.raises(sqlite3.DatabaseError):
        ksa.session_tasks(["s1"])


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


# --- cards on ANOTHER gateway, seen through the hermes-hub replica (hub.db) [daveey] ---

import json as _json


def _make_replica(state_dir: Path, me: str, tasks: list[dict]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "hub.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
        "CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,"
        " updated_at INTEGER, PRIMARY KEY (gw, coll, key));"
    )
    conn.execute("INSERT INTO meta VALUES ('gateway', ?)", (me,))
    for t in tasks:
        gw = t.pop("_gw")
        data = {"board": "default", "children": [], "parents": [], "sessions": [], "block_kind": None, **t}
        conn.execute("INSERT INTO replica VALUES (?, 'tasks', ?, 1, 1, ?, 0)",
                     (gw, f"{data['board']}/{data['id']}", _json.dumps(data, sort_keys=True)))
    conn.commit()
    conn.close()
    return path


def _shep(session, gw="macbook"):
    return {"session": session, "profile": "default", "gw": gw, "rel": "shepherd"}


def test_replica_card_shepherded_by_a_local_session_keeps_it_busy(kanban_root):
    _make_replica(kanban_root / "hub-state", "macbook", [
        {"_gw": "hub", "id": "t_r1", "status": "running", "children": ["t_r2"], "sessions": [
            {"session": "hub_oneshot", "profile": None, "rel": "created"}, _shep("s_mac")]},
        {"_gw": "hub", "id": "t_r2", "status": "todo", "children": ["t_r3"]},  # child: counts via children
        {"_gw": "hub", "id": "t_r3", "status": "blocked", "block_kind": "needs_input"},  # grandchild waits on a human
        {"_gw": "hub", "id": "t_r4", "status": "done", "sessions": [_shep("s_mac")]},
    ])
    rows = [{"id": "s_mac"}, {"id": "hub_oneshot"}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0]["kanban_active"] == 2
    assert rows[0]["kanban_blocked"] == 1
    assert "kanban_active" not in rows[1]  # a session on the owning gateway is not ours


def test_replica_link_for_another_gateway_does_not_count(kanban_root):
    _make_replica(kanban_root / "hub-state", "macbook", [
        {"_gw": "hub", "id": "t_r1", "status": "running", "sessions": [_shep("s_mac", gw="mini")]},
        {"_gw": "mini", "id": "t_r2", "status": "running", "sessions": [{"session": "s_mac", "rel": "created"}]},
        # our own published records in the replica are already counted from the local boards
        {"_gw": "macbook", "id": "t_r3", "status": "running", "sessions": [_shep("s_mac")]},
    ])
    rows = [{"id": "s_mac"}]
    ksa.annotate_kanban_activity(rows)
    assert "kanban_active" not in rows[0]


def test_replica_counts_add_to_local_boards_and_follow_lineage(kanban_root):
    _make_board(kanban_root / "kanban.db", [("t1", "running", "s_root")])
    _make_replica(kanban_root / "hub-state", "macbook", [
        {"_gw": "hub", "id": "t_r1", "status": "ready", "sessions": [_shep("s_root")]},
        {"_gw": "hub", "id": "t_r2", "status": "blocked", "block_kind": "transient", "sessions": [_shep("s_tip")]},
    ])
    rows = [{"id": "s_tip", "_lineage_root_id": "s_root", "_lineage_ids": ["s_root", "s_tip"]}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0]["kanban_active"] == 3
    assert "kanban_blocked" not in rows[0]


def test_absent_replica_is_optional_but_corrupt_replica_is_unknown(kanban_root):
    _make_board(kanban_root / "kanban.db", [("t1", "running", "s1")])
    rows = [{"id": "s1"}]
    ksa.annotate_kanban_activity(rows)  # no hub-state dir at all
    assert rows[0]["kanban_active"] == 1
    assert not (kanban_root / "hub-state" / "hub.db").exists()  # the read never creates the file
    (kanban_root / "hub-state").mkdir()
    (kanban_root / "hub-state" / "hub.db").write_bytes(b"not a database" * 100)
    ksa._reset_cache_for_tests()
    rows = [{"id": "s1"}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0].get("kanban_unknown") is True
    assert "kanban_active" not in rows[0]
    with pytest.raises(sqlite3.DatabaseError):
        ksa.session_tasks(["s1"])
    # a replica row with junk data is skipped, not fatal
    (kanban_root / "hub-state" / "hub.db").unlink()
    path = _make_replica(kanban_root / "hub-state", "macbook", [
        {"_gw": "hub", "id": "t_ok", "status": "running", "sessions": [_shep("s1")]}])
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO replica VALUES ('hub', 'tasks', 'default/t_bad', 1, 1, '{not json', 0)")
    conn.commit()
    conn.close()
    ksa._reset_cache_for_tests()
    rows = [{"id": "s1"}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0]["kanban_active"] == 2


def test_hub_platform_subscription_on_a_local_board_is_not_a_tui_shepherd(kanban_root):
    """The owning gateway stores a remote shepherd as platform 'hub'; its own sidebar must not count it."""
    _make_board(kanban_root / "kanban.db", [("t1", "running", None)])
    conn = sqlite3.connect(kanban_root / "kanban.db")
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT)")
    conn.execute("INSERT INTO kanban_notify_subs VALUES ('t1', 'hub', 'macbook/default/s_mac')")
    conn.commit()
    conn.close()
    assert ksa.active_task_counts() == {}


# --- one origin-work association for counts AND the task list (cross-gateway ownership) [daveey] ---

def _set_meta(path: Path, **kv) -> None:
    conn = sqlite3.connect(path)
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in kv.items()])
    conn.commit()
    conn.close()


def _dispatch_graph(root_status="running"):
    """A local chat on 'macbook' dispatched a 13-card graph that runs on 'hub': only the root carries the
    shepherd link (later cards are its descendants). Root running, 12 waiting on it."""
    kids = [f"t_c{i:02d}" for i in range(12)]
    cards = [{"_gw": "hub", "id": "t_root", "title": "P0", "status": root_status, "children": kids,
              "sessions": [{"session": "hub_orch", "profile": None, "rel": "created"}, _shep("s_origin")]}]
    cards += [{"_gw": "hub", "id": k, "title": k, "status": "todo", "parents": ["t_root"],
               "sessions": [{"session": "hub_orch", "profile": None, "rel": "created"}]} for k in kids]
    return cards


def test_remote_dispatch_graph_lists_every_owner_qualified_row_and_counts_the_same(kanban_root):
    """Dispatcher exited, origin chat keeps all 13 remote rows in its task list, and the sidebar count is the
    same set (one predicate), so the row cannot paint done."""
    _make_replica(kanban_root / "hub-state", "macbook", _dispatch_graph())
    rows = ksa.session_tasks(["s_origin"])
    assert len(rows) == 13
    assert {(r["gateway"], r["board"], r["id"]) for r in rows} == (
        {("hub", "default", "t_root")} | {("hub", "default", f"t_c{i:02d}") for i in range(12)})
    root = next(r for r in rows if r["id"] == "t_root")
    assert root["remote"] is True and root["via"] == "shepherd" and root["status"] == "running"
    assert next(r for r in rows if r["id"] == "t_c00")["via"] == "descendant"
    assert all(r["key"] == f"hub/default/{r['id']}" for r in rows)
    ann = [{"id": "s_origin"}]
    ksa.annotate_kanban_activity(ann)
    assert ann[0]["kanban_active"] == 13


def test_done_root_still_lists_unfinished_descendants(kanban_root):
    _make_replica(kanban_root / "hub-state", "macbook", _dispatch_graph(root_status="done"))
    rows = ksa.session_tasks(["s_origin"])
    assert sorted(r["id"] for r in rows) == [f"t_c{i:02d}" for i in range(12)]
    ann = [{"id": "s_origin"}]
    ksa.annotate_kanban_activity(ann)
    assert ann[0]["kanban_active"] == 12


def test_review_is_unfinished_on_local_and_remote_cards(kanban_root):
    """A card waiting for code review (or a goal completion awaiting verification) is not done."""
    _make_board(kanban_root / "kanban.db", [("t_l", "review", "s1")])
    _make_replica(kanban_root / "hub-state", "macbook", [
        {"_gw": "hub", "id": "t_r", "status": "review", "sessions": [_shep("s1")]}])
    ann = [{"id": "s1"}]
    ksa.annotate_kanban_activity(ann)
    assert ann[0]["kanban_active"] == 2
    assert {r["id"] for r in ksa.session_tasks(["s1"])} == {"t_l", "t_r"}


def test_same_task_id_on_two_gateways_is_two_rows(kanban_root):
    _make_board(kanban_root / "kanban.db", [("t_same", "running", "s1")])
    _make_replica(kanban_root / "hub-state", "macbook", [
        {"_gw": "hub", "id": "t_same", "status": "running", "sessions": [_shep("s1")]},
        {"_gw": "mini", "id": "t_same", "status": "todo", "sessions": [_shep("s1")]}])
    ann = [{"id": "s1"}]
    ksa.annotate_kanban_activity(ann)
    assert ann[0]["kanban_active"] == 3
    rows = ksa.session_tasks(["s1"])
    assert sorted((r.get("gateway"), r["id"]) for r in rows if r.get("remote")) == [("hub", "t_same"), ("mini", "t_same")]
    assert len({r["key"] for r in rows}) == 3


def test_stale_replica_keeps_last_known_unfinished_rows_marked_stale(kanban_root):
    path = _make_replica(kanban_root / "hub-state", "macbook", _dispatch_graph())
    _set_meta(path, last_ok=1.0, offline_since=2.0)  # hub unreachable for ages
    rows = ksa.session_tasks(["s_origin"])
    assert len(rows) == 13 and all(r["stale"] for r in rows)
    ann = [{"id": "s_origin"}]
    ksa.annotate_kanban_activity(ann)
    assert ann[0]["kanban_active"] == 13
    assert ann[0]["kanban_remote_stale"] == 13


def test_failed_replica_read_keeps_the_last_good_answer_never_zero(kanban_root):
    path = _make_replica(kanban_root / "hub-state", "macbook", _dispatch_graph())
    import time as _time
    _set_meta(path, last_ok=_time.time())
    ann = [{"id": "s_origin"}]
    ksa.annotate_kanban_activity(ann)
    assert ann[0]["kanban_active"] == 13 and "kanban_remote_stale" not in ann[0]
    path.write_bytes(b"not a database" * 100)  # replica now unreadable
    ksa._reset_cache_for_tests(keep_remote=True)
    ann = [{"id": "s_origin"}]
    ksa.annotate_kanban_activity(ann)
    assert ann[0]["kanban_active"] == 13
    assert ann[0]["kanban_remote_stale"] == 13
    assert all(r["stale"] for r in ksa.session_tasks(["s_origin"]))


def test_missing_replica_after_restart_retains_origin_work(kanban_root):
    path = _make_replica(kanban_root / "hub-state", "macbook", _dispatch_graph())
    import time as _time
    _set_meta(path, last_ok=_time.time())
    assert len(ksa.session_tasks(["s_origin"])) == 13
    path.unlink()
    ksa._reset_cache_for_tests()  # all process memory gone, as after restart
    rows = [{"id": "s_origin"}, {"id": "s_unrelated"}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0].get("kanban_active") == 13
    assert rows[0].get("kanban_remote_stale") == 13
    assert rows[1] == {"id": "s_unrelated"}
    cards = ksa.session_tasks(["s_origin"])
    assert len(cards) == 13
    assert all(card["stale"] for card in cards)


def test_empty_replica_does_not_erase_obligations_after_restart(kanban_root):
    path = _make_replica(kanban_root / "hub-state", "macbook", _dispatch_graph())
    import time as _time
    _set_meta(path, last_ok=_time.time())
    assert len(ksa.session_tasks(["s_origin"])) == 13
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM replica")
    ksa._reset_cache_for_tests()
    cards = ksa.session_tasks(["s_origin"])
    assert len(cards) == 13
    assert all(card["stale"] for card in cards)


def test_final_completion_clears_the_origin_and_other_sessions_finish_normally(kanban_root):
    graph = _dispatch_graph(root_status="done")
    for c in graph:
        c["status"] = "done"
    _make_replica(kanban_root / "hub-state", "macbook", graph)
    _make_board(kanban_root / "kanban.db", [("t_x", "done", "s_other")])
    ann = [{"id": "s_origin"}, {"id": "s_other"}]
    ksa.annotate_kanban_activity(ann)
    assert ann == [{"id": "s_origin"}, {"id": "s_other"}]
    assert ksa.session_tasks(["s_origin"]) == []


def test_failed_activity_read_stamps_unknown_not_idle(kanban_root, monkeypatch):
    """A failed kanban read is not "no work": the row says ``kanban_unknown`` so the sidebar can't paint it
    done, and the stamp clears once a read succeeds. [daveey]"""
    def boom():
        raise RuntimeError("replica and boards unreadable")

    monkeypatch.setattr(ksa, "_all_counts", boom)
    rows = [{"id": "s1", "kanban_active": 3}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0].get("kanban_unknown") is True
    assert "kanban_active" not in rows[0]  # no stale count presented as fresh
    monkeypatch.undo()
    ksa._reset_cache_for_tests()
    ksa.annotate_kanban_activity(rows)
    assert "kanban_unknown" not in rows[0]
