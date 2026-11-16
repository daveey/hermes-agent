"""Replica-only graph endpoints are foreign, not nonexistent board cards."""
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.tools import test_kanban_show_replica as fixtures
from tools import kanban_tools as kt

boards = fixtures.boards


def test_link_foreign_child_routes_to_owner(boards):
    child, _ = boards
    with kbc.connect() as live:
        parent = kb.create_task(live, title="Local parent")
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert out.get("rejection") == "foreign_card", out
    assert child in out["error"] and "owner-gateway" in out["error"]
    assert "If the child is remote, ask its owner" in out["error"]
    with kbc.connect() as live:
        assert kb.child_ids(live, parent) == []


def test_link_local_endpoints_still_gates_child(boards):
    with kbc.connect() as live:
        parent = kb.create_task(live, title="Local parent")
        child = kb.create_task(live, title="Local child")
        kb.recompute_ready(live)
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert out.get("gated") is True, out
    with kbc.connect() as live:
        assert kb.parent_ids(live, child) == [parent]
        assert kb.get_task(live, child).status == "todo"


def test_link_unknown_endpoint_stays_unknown(boards):
    with kbc.connect() as live:
        child = kb.create_task(live, title="Local child")
    out = json.loads(kt._handle_link({"parent_id": "t_missing", "child_id": child}))
    assert "unknown task(s): t_missing" in out["error"]
    assert out.get("rejection") != "foreign_card"


def test_link_disabled_sync_stays_unknown(boards, monkeypatch):
    parent, _ = boards
    monkeypatch.setattr(fixtures.sync, "sync_config", lambda: {"enabled": False})
    with kbc.connect() as live:
        child = kb.create_task(live, title="Local child")
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert "unknown task(s)" in out["error"]
    assert out.get("rejection") != "foreign_card"


def test_link_foreign_parent_names_owner_without_writing(boards):
    parent, shadow = boards
    with kbc.connect() as live:
        child = kb.create_task(live, title="Local child")
        before = live.execute("SELECT * FROM tasks WHERE id = ?", (child,)).fetchone()
        events = live.execute("SELECT count(*) FROM task_events").fetchone()[0]
    replica_before = shadow.read_bytes()
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": child}))
    assert out.get("rejection") == "foreign_card", out
    assert parent in out["error"] and "owner-gateway" in out["error"]
    assert "unknown task(s)" not in out["error"]
    assert "Do not retry" in out["error"]
    assert "awaiting" in out["error"]
    assert shadow.read_bytes() == replica_before
    with kbc.connect() as live:
        assert kb.get_task(live, parent) is None
        assert tuple(live.execute("SELECT * FROM tasks WHERE id = ?", (child,)).fetchone()) == tuple(before)
        assert live.execute("SELECT count(*) FROM task_events").fetchone()[0] == events
        assert kb.parent_ids(live, child) == []
