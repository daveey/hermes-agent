"""Real replica membership -> closure -> persisted index -> parent-only push. [daveey]"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_sync_client as sync
from hermes_session_state_events import BoardWatcher
from tests.session_state.conftest import drain


@pytest.fixture
def replica(store, kanban_root, clock, monkeypatch):
    path = kanban_root / "kanban" / "sync-shadow" / "default.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
            "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT);"
            "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, "
            "chat_session TEXT, kind TEXT, released INTEGER, created_at INTEGER);"
            "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);"
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at REAL);"
            "CREATE TABLE kanban_sync_state (pulled_at REAL);")
        conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, 'worker', 1, NULL, 0, NULL, NULL, ?)",
                         [("child", "Ship", "todo", "remote"), ("parent", "Build", "running", "remote")])
        conn.execute("INSERT INTO task_chat_links VALUES ('link', 'child', 'local', 'default', 'chat', 'created', 0, 1)")
        conn.execute("INSERT INTO task_links VALUES ('parent', 'child')")
        conn.execute("INSERT INTO kanban_sync_state VALUES (?)", (clock.now,))

    def listing(ids=None, statuses=(), stale_ok=False):
        return links.replica_cards(ids, "local", statuses, replicas=[("default", path)], profile="default",
                                   origin_cards=[], now=clock.now, stale_ok=stale_ok)

    monkeypatch.setattr(activity, "_remote_cards", listing)
    monkeypatch.setattr(sync, "shadow_path", lambda board: path)
    watcher = BoardWatcher(store, replicas=lambda: [path], remote_cards=listing)
    store.add_tick(watcher)
    drain(store)
    yield path


def test_replica_parent_only_event_pushes_through_real_reader_and_index(store, replica):
    before = store.recompute("chat")
    child, parent = "remote/default/default/child", "remote/default/default/parent"
    assert before["cards"][child]["parents"] == [parent]
    assert before["cards"][parent]["linked"] is False
    assert store.lineages_for_card(parent) == ["chat"]
    count = len(store.pushed)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id='parent'")
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('parent','completed','{}',2)")
    drain(store)
    assert len(store.pushed) > count
    after = store.get("chat")
    assert after["rev"] > before["rev"]
    assert after["cards"][parent]["lifecycle"] == "done"
    assert after["cards"][child]["parents"] == [parent]


@pytest.mark.parametrize("stamp", [None, -1, float("inf"), 1, 1e15])
def test_replica_freshness_never_promotes_bad_or_expired_source(store, replica, stamp):
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE kanban_sync_state SET pulled_at=?", (stamp,))
    obj = store.recompute("chat")
    parent = obj["cards"]["remote/default/default/parent"]
    assert parent["source"]["freshness"] == "unknown"
    assert parent["blocked_on"] == "unknown"
    assert not parent["red"]


def test_replica_source_expiry_uses_next_change_timer(store, replica, clock):
    obj = store.recompute("chat")
    assert obj["next_change_at"] > clock.now
    clock.now = obj["next_change_at"] + 0.01
    drain(store)
    after = store.get("chat")
    assert after["rev"] > obj["rev"]
    assert after["cards"]["remote/default/default/parent"]["blocked_on"] == "unknown"


def test_missing_parent_is_unknown_indexed_then_arrival_pushes(store, replica):
    with sqlite3.connect(replica) as conn:
        conn.execute("DELETE FROM tasks WHERE id='parent'")
    obj = store.recompute("chat")
    missing = "?/default/default/parent"
    assert obj["cards"][missing]["blocked_on"] == "unknown"
    assert store.lineages_for_card(missing) == ["chat"]
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO tasks VALUES ('parent','Arrived','running','worker',1,NULL,0,NULL,NULL,'owner2')")
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('parent','created','{}',2)")
    drain(store)
    after = store.get("chat")
    assert missing not in after["cards"]
    assert after["cards"]["owner2/default/default/parent"]["title"] == "Arrived"


@pytest.mark.parametrize("remote", [False, True])
def test_local_arrival_wakes_unknown_dependency_without_borrowing_owner(store, board, replica, remote):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    if remote:
        child = "remote/default/default/child"
        with sqlite3.connect(replica) as conn:
            conn.execute("DELETE FROM tasks WHERE id='parent'")
    else:
        tid = kb.create_task(board, title="Local child", assignee="worker", session_id="chat")
        child = f"default/{tid}"
        with write_txn(board):
            board.execute("INSERT INTO task_links(parent_id,child_id) VALUES ('later',?)", (tid,))
    pid = "parent" if remote else "later"
    missing = f"?/default/default/{pid}"
    drain(store)  # Consume setup events before the parent-only arrival.
    before = store.recompute("chat")
    assert before["cards"][child]["parents"] == [missing]
    assert store.lineages_for_card(missing) == ["chat"]
    count = len(store.pushed)
    with write_txn(board):
        board.execute("INSERT INTO tasks(id,title,status,assignee,created_at) VALUES (?, 'Arrived','done','worker',1)",
                      (pid,))
        board.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'completed','{}',2)", (pid,))
    drain(store)
    after = store.get("chat")
    if remote:
        assert after["cards"][child]["parents"] == [missing]
        assert after["cards"][missing]["blocked_on"] == "unknown"
        assert f"default/{pid}" not in after["cards"]
        assert after["rev"] == before["rev"]
    else:
        assert len(store.pushed) > count
        assert after["rev"] > before["rev"]
        assert missing not in after["cards"]
        assert after["cards"][child]["parents"] == [f"default/{pid}"]
        assert after["cards"][f"default/{pid}"]["lifecycle"] == "done"
        assert store.lineages_for_card(missing) == []
        assert store.lineages_for_card(f"default/{pid}") == ["chat"]


def test_local_same_id_cannot_replace_remote_parent(store, replica, board):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    local = kb.create_task(board, title="Unrelated local", assignee="worker", session_id="chat")
    with write_txn(board):
        board.execute("UPDATE tasks SET id='parent' WHERE id=?", (local,))
    obj = store.recompute("chat")
    assert obj["cards"]["default/parent"]["title"] == "Unrelated local"
    assert obj["cards"]["remote/default/default/parent"]["title"] == "Build"
    assert obj["cards"]["remote/default/default/child"]["parents"] == ["remote/default/default/parent"]


def test_local_child_uses_shadow_parent_but_local_unlink_beats_stale_shadow(store, replica, board):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    local = kb.create_task(board, title="Local ship", assignee="worker", session_id="chat")
    with write_txn(board):
        board.execute("INSERT INTO task_links(parent_id,child_id) VALUES ('parent',?)", (local,))
    obj = store.recompute("chat")
    assert obj["cards"][f"default/{local}"]["parents"] == ["remote/default/default/parent"]
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO tasks VALUES (?, 'Stale child','todo','worker',1,NULL,0,NULL,NULL,'local')", (local,))
        conn.execute("INSERT INTO task_links VALUES ('parent',?)", (local,))
    with write_txn(board):
        board.execute("DELETE FROM task_links WHERE child_id=?", (local,))
    after = store.recompute("chat")
    assert after["cards"][f"default/{local}"]["parents"] == []


def test_remote_root_owner_mismatch_and_named_profile_do_not_borrow_shadow(replica):
    from hermes_session_state_graph import collect_graph

    for gateway, profile in [("other", "default"), ("remote", "named")]:
        key = f"{gateway}/{profile}/default/child"
        graph = collect_graph([{"id": "child", "board": "default", "gateway": gateway,
                                "profile": profile, "key": key, "status": "todo"}], {}, "local")
        assert set(graph["rows"]) == {key}
        assert graph["rows"][key]["partial_source_unknown"] == "dependency_source_unavailable"
        assert graph["parents"][key] == []


def test_missing_replica_retains_linked_root_as_unknown(store, replica, monkeypatch):
    monkeypatch.setattr(sync, "shadow_path", lambda board: replica.parent / "missing.db")
    obj = store.recompute("chat")
    row = obj["cards"]["remote/default/default/child"]
    assert row["blocked_on"] == "unknown"
    assert row["parents"] == []
    assert any(s["coverage"] == "unknown" for k, s in obj["sources"].items() if k.startswith("closure:"))


def test_unknown_owner_cannot_be_live(store, replica):
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE tasks SET created_on=NULL WHERE id='parent'")
    obj = store.recompute("chat")
    row = obj["cards"]["?/default/default/parent"]
    assert row["blocked_on"] == "unknown"
    assert row["source"]["freshness"] == "unknown"


def test_multihop_cycle_keeps_each_qualified_card_once(store, replica):
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO tasks VALUES ('grand','Grand','running','worker',1,NULL,0,NULL,NULL,'owner2')")
        conn.executemany("INSERT INTO task_links VALUES (?,?)", [("grand", "parent"), ("child", "grand")])
    obj = store.recompute("chat")
    assert len(obj["cards"]) == 3
    assert obj["cards"]["remote/default/default/parent"]["parents"] == ["owner2/default/default/grand"]
    assert obj["cards"]["owner2/default/default/grand"]["parents"] == ["remote/default/default/child"]
    assert store.lineages_for_card("owner2/default/default/grand") == ["chat"]


@pytest.mark.parametrize("limit", ["MAX_DEPTH", "MAX_CLOSURE"])
def test_closure_bound_reports_unknown_without_dropping_linked_root(store, replica, monkeypatch, limit):
    import hermes_session_state_graph as graph

    monkeypatch.setattr(graph, limit, 0)
    obj = store.recompute("chat")
    assert "remote/default/default/child" in obj["cards"]
    assert "remote/default/default/parent" not in obj["cards"]
    assert any(s["reason"] == "closure_truncated" for s in obj["sources"].values())


def test_unreadable_edges_mark_even_partially_collected_root_unknown(store, replica):
    with sqlite3.connect(replica) as conn:
        conn.execute("DROP TABLE task_links")
    obj = store.recompute("chat")
    row = obj["cards"]["remote/default/default/child"]
    assert row["blocked_on"] == "unknown"
    assert obj["sources"]["closure:default"]["coverage"] == "unknown"
