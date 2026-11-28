"""One test per SessionState v2 event source: event -> recompute -> pushed object. [daveey]

Every source is exercised through its real entry point (a board commit from another connection, a Board replica
file, ``refresh_outcome``, a state.db transcript write, the process registry's lifecycle wake, the user-steps
request store), then the worker pass the gateway thread runs."""
from __future__ import annotations

import sqlite3

import hermes_state_outcome as so
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_session_activity as ksa
from hermes_session_state_events import (
    BoardWatcher, ProcessWatcher, StateWatcher, UserStepsWatcher, emit_session,
)
from tests.session_state.conftest import HUMAN_REQUEST, drain
from tools.process_registry import ProcessSession
from tools.process_registry_events import publish


def _last(store, lineage="chat"):
    mine = [o for o in store.pushed if o["identity"]["lineage"] == lineage]
    assert mine, f"nothing pushed for {lineage}"
    return mine[-1]


def test_board_task_events_commit_from_another_connection(store, board, kanban_root):
    watcher = BoardWatcher(store, replicas=list, remote_cards=list)
    store.add_tick(watcher)
    drain(store)  # baseline cursors
    other = sqlite3.connect(kanban_root / "kanban.db")
    other.close()
    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")  # never indexed before
    drain(store)
    assert f"default/{task}" in _last(store)["cards"]
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    drain(store)
    assert _last(store)["cards"][f"default/{task}"]["blocked_on"] == "human"


def test_a_closure_parent_event_reaches_the_child_session_through_the_index(store, board):
    watcher = BoardWatcher(store, replicas=list, remote_cards=list)
    store.add_tick(watcher)
    parent = kb.create_task(board, title="Build", assignee="worker")
    kb.create_task(board, title="Ship", assignee="worker", session_id="chat", parents=[parent])
    drain(store)
    store.recompute("chat")
    before = len(store.pushed)
    assert kb.complete_task(board, parent, summary="built")  # no chat is linked to the parent itself
    drain(store)
    assert len(store.pushed) > before
    assert _last(store)["cards"][f"default/{parent}"]["lifecycle"] == "done"


def test_replica_sync_arrival(store, kanban_root, monkeypatch):
    shadow = kanban_root / "kanban" / "sync-shadow" / "hub.db"
    shadow.parent.mkdir(parents=True)
    with sqlite3.connect(shadow) as conn:
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)")
    remote: list[dict] = []

    def listing(ids=None, statuses=(), stale_ok=False):
        return [c for c in remote if ids is None or c["session"] in ids]
    monkeypatch.setattr(ksa, "_remote_cards", listing)
    watcher = BoardWatcher(store, boards=dict, replicas=lambda: [shadow], remote_cards=listing)
    store.add_tick(watcher)
    drain(store)
    remote.append({"key": "hub/default/t_remote", "id": "t_remote", "board": "default", "gateway": "hub",
                   "session": "chat", "status": "running", "title": "Remote work", "stale": False,
                   "synced_at": 1.0})
    with sqlite3.connect(shadow) as conn:
        conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES ('t_remote', 'claimed', '{}')")
    drain(store)
    card = _last(store)["cards"]["hub/default/t_remote"]
    assert (card["gateway"], card["lifecycle"]) == ("hub", "running")
    remote[0] = {**remote[0], "status": "done"}  # a later replica event for the indexed key alone
    with sqlite3.connect(shadow) as conn:
        conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES ('t_remote', 'completed', '{}')")
    watcher._remote = None  # only the by-id index path may deliver this one
    drain(store)
    assert "hub/default/t_remote" not in _last(store)["cards"]  # done leaves the listed cards


def test_turn_start_and_end_through_refresh_outcome(store, db):
    so.refresh_outcome("chat", "turn_start", db=db)
    drain(store)
    assert _last(store)["identity"]["tip"] == "chat"
    db.append_message("chat", role="user", content="please deploy")
    so.refresh_outcome("chat", "turn_end", db=db)
    drain(store)
    assert store.metrics()["recomputes"] >= 2


def test_state_db_transcript_write_from_another_process(store, db, tmp_path):
    watcher = StateWatcher(store, tmp_path / "state.db")
    store.add_tick(watcher)
    drain(store)
    db.create_session("child", source="subagent", parent_session_id="chat")
    db.append_message("child", role="user", content="sub task")
    drain(store)
    parent = _last(store)
    assert [s["id"] for s in parent["session"]["subagents"]] == ["subagent:child"]


def test_process_lifecycle_wake(store, registry):
    watcher = ProcessWatcher(store, registry)
    store.add_tick(watcher)
    drain(store)
    live = ProcessSession(id="proc_live", command="sleep 60", cwd="/", task_id="task", parent_session_id="chat")
    registry._running[live.id] = live
    publish(registry)
    drain(store)
    assert _last(store)["session"]["processes"] == [{"id": "process:proc_live", "state": "running"}]
    assert _last(store)["session"]["mode"] == "running"
    watcher.close()


def test_subagent_reap_through_refresh_outcomes_quietly(store, db):
    db.create_session("child", source="subagent", parent_session_id="chat")
    so.refresh_outcomes_quietly(["chat"], "reap", db=db)
    drain(store)
    assert _last(store)["session"]["subagents"]


def test_user_steps_request_create_and_resolve(store, tmp_path, monkeypatch):
    path = tmp_path / "user-steps" / "actions.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE requests (namespace TEXT, id TEXT, session TEXT, revision INTEGER, state TEXT, "
                     "resolution TEXT, PRIMARY KEY(namespace, id))")

    def provider(sid):
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT id FROM requests WHERE session = ? AND state = 'open'", (sid,)).fetchall()
        return {"decision_items": [{"id": f"us:{r[0]}", "question": "Pick a colour"} for r in rows]}
    monkeypatch.setitem(so._providers, "user-steps-test", provider)
    watcher = UserStepsWatcher(store, path)
    store.add_tick(watcher)
    store.recompute("chat")
    drain(store)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO requests VALUES ('ns', 'r1', 'chat', 1, 'open', NULL)")
    drain(store)
    assert [a["id"] for a in _last(store)["asks"]] == ["us:r1"]
    assert _last(store)["header"]["status"] == "needs_you"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE requests SET state = 'resolved', resolution = 'blue' WHERE id = 'r1'")
    drain(store)
    assert _last(store)["asks"] == []


def test_emit_session_without_a_running_store_is_a_no_op(monkeypatch):
    from hermes_session_state_store import install

    previous = install(None)
    try:
        emit_session("chat", "manual")  # must not raise
    finally:
        install(previous)
