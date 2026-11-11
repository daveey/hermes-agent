"""Tool provenance survives local, gateway and remote blocked notices."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban, kanban_db as db
from hermes_cli.kanban_db_connect import connect_closing
from tests.tools import test_kanban_tools as helpers

worker_env = helpers.worker_env
REQUEST = dict(action="Choose A. Unblock = proceed; archive = abandon.",
               why_human="Owner decision", interaction="reply", scope="Fixture", evidence="Fixture decision")


def notices(conn, tid, event):
    from gateway.kanban_watchers_notifier import _fmt_blocked
    from hermes_cli.kanban_origin_ledger import observe_owner_task
    from tui_gateway.kanban_remote_notices import format_notice
    from tui_gateway.session_notifications import _format_kanban_event_text
    task = db.get_task(conn, tid)
    local = _format_kanban_event_text({"task_id": tid}, task, event, "default")
    gateway, wake, _ = _fmt_blocked(event, SimpleNamespace(task=task, task_id=tid, board_slug="default", head=tid))
    with db.write_txn(conn):
        projected = observe_owner_task(conn, {"id": tid, "status": "blocked", "human_request": REQUEST}, now=0)
    return local, gateway, wake, format_notice("other-host", "default/" + tid, projected)


@pytest.mark.parametrize("worker", [True, False])
def test_tool_actor_stored_and_rendered(worker_env, monkeypatch, worker):
    from tools import kanban_tools  # register the real handler
    from tools.registry import registry
    tid = worker_env
    monkeypatch.setenv("HERMES_SESSION_ID", "fixture-chat")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    if not worker:
        monkeypatch.delenv("HERMES_KANBAN_TASK")
    result = json.loads(registry.dispatch("kanban_block", {
        "task_id": tid, "reason": "Fixture decision", "kind": "needs_input", "human_request": REQUEST,
    }))
    assert "error" not in result, result
    expected = f"agent (task {tid})" if worker else "agent (chat fixture-chat)"
    with connect_closing() as conn:
        event = db.list_events(conn, tid)[-1]
        assert event.payload.get("actor") == expected
        for text in notices(conn, tid, event):
            assert f"actor: {expected}" in text
            assert "actor: user" not in text


def test_tool_without_identity_is_still_agent(monkeypatch):
    from hermes_cli.kanban_cli_actor import tool_actor
    for key in (*kanban._AGENT_ENV_MARKERS, "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    assert tool_actor() == "agent"


@pytest.mark.parametrize("session,key,expected", [
    ("scoped-chat", "scoped-key", "agent (chat scoped-chat)"),
    ("", "scoped-key", "agent (chat scoped-key)"),
    ("", "", "agent"),
])
def test_tool_actor_uses_request_scope(monkeypatch, session, key, expected):
    from gateway.session_context import scoped_current_session_id, scoped_session_key
    from hermes_cli.kanban_cli_actor import tool_actor
    monkeypatch.setenv("HERMES_SESSION_ID", "foreign-chat")
    monkeypatch.setenv("HERMES_SESSION_KEY", "foreign-key")
    with scoped_current_session_id(session), scoped_session_key(key):
        assert tool_actor() == expected
        assert tool_actor(worker_task="owned-task") == "agent (task owned-task)"


def test_no_request_notice_unchanged():
    from tui_gateway.session_notifications import _format_kanban_event_text
    with connect_closing() as conn:
        tid = db.create_task(conn, title="No request fixture", assignee="owner")
        db._append_event(conn, tid, "blocked", {"reason": "legacy reason", "kind": "transient"})
        event = db.list_events(conn, tid)[-1]
        task = db.get_task(conn, tid)
        before = _format_kanban_event_text({"task_id": tid}, task, event, "default")
        event.payload["actor"] = "agent (chat fixture-chat)"
        assert _format_kanban_event_text({"task_id": tid}, task, event, "default") == before
