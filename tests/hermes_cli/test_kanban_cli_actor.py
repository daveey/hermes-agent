"""CLI audit actor is the caller, not the recipient of a human request."""
import argparse
import json
import os

import pytest

from hermes_cli import kanban, kanban_db as db
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_parser import build_parser
from tui_gateway.session_notifications import _format_kanban_event_text

REQUEST = dict(action="Choose A. Unblock = proceed; archive = abandon.",
               why_human="Owner decision", interaction="reply", scope="Fixture", evidence="Fixture decision")


def cli(*args):
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(["kanban", *args]))


@pytest.fixture(autouse=True)
def isolated_actor(monkeypatch):
    for key in (*kanban._AGENT_ENV_MARKERS, "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    assert not os.environ.get("HERMES_KANBAN_TASK")


@pytest.mark.parametrize("marker", [None, "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_KANBAN_WORKER_TASK",
                                    "HERMES_RPC_SOCKET", "HERMES_KERNEL_SENTINEL", "HERMES_SESSION_SOURCE"])
@pytest.mark.parametrize("metadata_only", [False, True])
def test_cli_block_actor_and_notice(monkeypatch, marker, metadata_only):
    if marker:
        monkeypatch.setenv(marker, "fixture-chat-123")
    with connect_closing() as conn:
        tid = db.create_task(conn, title="Actor fixture", assignee="owner",
                             initial_status="blocked" if metadata_only else "running", lint_blocked=False)
    flags = ["--metadata-only"] if metadata_only else ["--kind", "needs_input"]
    assert cli("block", tid, "Fixture decision", *flags, "--human-request", json.dumps(REQUEST)) == 0
    with connect_closing() as conn:
        event = db.list_events(conn, tid)[-1]
        task = db.get_task(conn, tid)
    actor = event.payload["actor"]
    assert actor.startswith("agent") if marker else actor == "user"
    if marker in {"HERMES_SESSION_ID", "HERMES_SESSION_KEY"}:
        assert "chat fixture-chat-123" in actor
    text = _format_kanban_event_text({"task_id": tid}, task, event, "default")
    assert f"actor: {actor}" in text
    from hermes_cli.kanban_origin_ledger import observe_owner_task
    from tui_gateway.kanban_remote_notices import format_notice
    with connect_closing() as conn, db.write_txn(conn):
        projected = observe_owner_task(conn, {"id": tid, "status": "blocked", "human_request": REQUEST}, now=0)
    remote = format_notice("other-host", "default/" + tid, projected)
    assert f"actor: {actor}" in remote
    assert "Next action for the user:" in text
    if marker:
        assert "actor: user" not in text
    assert cli("unblock", tid) == 0
    with connect_closing() as conn:
        assert db.list_events(conn, tid)[-1].payload["actor"] == actor


def test_human_cli_notice_control():
    """This existing behavior also passes on the unpatched baseline."""
    with connect_closing() as conn:
        tid = db.create_task(conn, title="Human fixture", assignee="owner")
    assert cli("block", tid, "Fixture", "--kind", "needs_input", "--human-request", json.dumps(REQUEST)) == 0
    with connect_closing() as conn:
        text = _format_kanban_event_text({"task_id": tid}, db.get_task(conn, tid), db.list_events(conn, tid)[-1], "default")
    assert "actor: user" in text


def test_untyped_classification_and_gateway_actor(monkeypatch):
    from types import SimpleNamespace
    from gateway.kanban_watchers_notifier import _fmt_blocked
    monkeypatch.setenv("HERMES_SESSION_ID", "actor-chat")
    with connect_closing() as conn:
        tid = db.create_task(conn, title="Untyped fixture", assignee="owner", initial_status="blocked", lint_blocked=False)
    assert cli("block", tid, "Fixture", "--kind", "needs_input", "--human-request", json.dumps(REQUEST)) == 0
    with connect_closing() as conn:
        event = db.list_events(conn, tid)[-1]
        task = db.get_task(conn, tid)
    assert event.payload["actor"] == "agent (chat actor-chat)"
    text, wake, _ = _fmt_blocked(event, SimpleNamespace(task=task, task_id=tid, board_slug="default", head=tid))
    assert "actor: agent (chat actor-chat)" in text
    assert "actor: agent (chat actor-chat)" in wake


def test_actor_session_precedence_and_worker_marker(monkeypatch):
    from hermes_cli.kanban_cli_actor import cli_actor
    monkeypatch.setenv("HERMES_KANBAN_TASK", "assigned-task")
    assert cli_actor() == "agent (task assigned-task)"
    monkeypatch.setenv("HERMES_SESSION_ID", "fallback-chat")
    assert cli_actor() == "agent (chat fallback-chat)"
    monkeypatch.setenv("HERMES_SESSION_KEY", "desktop-chat")
    assert cli_actor() == "agent (chat desktop-chat)"


@pytest.mark.parametrize("actor", [None, "", 123, "user", "agent (chat test)"])
def test_remote_actor_is_optional_audit_field(actor):
    from hermes_cli.kanban_block_notice import blocked_actor_fields, block_notice
    with connect_closing() as conn:
        tid = db.create_task(conn, title="Audit fixture", assignee="owner")
        assert blocked_actor_fields(conn, tid) == {}
        db._append_event(conn, tid, "blocked", {"actor": actor})
        fields = blocked_actor_fields(conn, tid)
    assert fields == ({"block_actor": actor} if isinstance(actor, str) and actor else {})
    if isinstance(actor, str) and actor:
        text = block_notice({"status": "blocked", "assignee": "owner"}, tid, "default", actor=actor)
        assert f"actor: {actor}" in text
        assert "Next action: the agent" in text
