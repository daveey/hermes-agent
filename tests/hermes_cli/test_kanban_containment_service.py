"""Public adapters share admitted identity; no caller-supplied actor authority."""
from __future__ import annotations

import argparse
import json

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_actor as fixtures

board = fixtures.board


def arguments(board, monkeypatch):
    relay, _, session = fixtures.bind(board, monkeypatch)
    authority = board.execute("SELECT id FROM tasks WHERE id!=? AND status='running'", (relay,)).fetchone()[0]
    return relay, authority, session


def test_service_contains_without_releasing_dependency(board, monkeypatch):
    from hermes_cli import kanban_containment_service as service
    relay, authority, session = arguments(board, monkeypatch)
    edges = list(board.execute("SELECT parent_id,child_id FROM task_links"))
    with scoped_current_session_id(session):
        result = service.contain(board, task_id=relay, authority=authority, summary="Fixture handoff")
    assert result["status"] == "triage"
    assert result["resume_policy"]["authority"] == {"board": "default", "gateway": "local", "task_id": authority}
    assert result["terminal_for_run"] is True
    assert result["dependency_satisfied"] is False
    assert list(board.execute("SELECT parent_id,child_id FROM task_links")) == edges
    assert board.execute("SELECT status FROM tasks WHERE id IN (SELECT child_id FROM task_links WHERE parent_id=?)", (relay,)).fetchone()[0] == "todo"


def test_tool_and_cli_reach_same_service(board, monkeypatch, capsys):
    from hermes_cli import kanban_containment_cli as cli
    from tools import kanban_containment_tools as tools
    from tools import kanban_tools
    relay, authority, session = arguments(board, monkeypatch)
    # Only the DB context is replaced; retain real parser, handler, actor and lifecycle.
    from contextlib import contextmanager

    @contextmanager
    def opened(*args, **kwargs):
        yield kb, board

    monkeypatch.setattr(kanban_tools, "_board", opened)
    with scoped_current_session_id(session):
        out = json.loads(tools.handle_contain({"task_id": relay, "authority": authority, "summary": "Fixture handoff"}))
    assert out["ok"] is True
    episode = out["episode"]
    current = kb.get_task(board, authority)
    for key, value in {"HERMES_KANBAN_TASK": authority, "HERMES_KANBAN_RUN_ID": str(current.current_run_id),
                       "HERMES_KANBAN_CLAIM_LOCK": current.claim_lock}.items():
        monkeypatch.setenv(key, value)
    kb._append_event(board, authority, "owner_admitted", {"session_id": session}, run_id=current.current_run_id)
    from hermes_cli import kanban_db_connect as kbc

    @contextmanager
    def connection(*args, **kwargs):
        yield board

    monkeypatch.setattr(kbc, "connect_closing", connection)
    with scoped_current_session_id(session):
        rc = cli.command(argparse.Namespace(kanban_action="resume-contained", task_id=relay, episode=episode,
                                            kind="authority", board=None))
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_unadmitted_tool_returns_machine_readable_refusal(board, monkeypatch):
    from tools import kanban_containment_tools as tools
    from tools import kanban_tools
    from contextlib import contextmanager
    relay, authority, _ = arguments(board, monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_TASK")

    @contextmanager
    def opened(*args, **kwargs):
        yield kb, board

    monkeypatch.setattr(kanban_tools, "_board", opened)
    result = json.loads(tools.handle_contain({"task_id": relay, "authority": authority, "summary": "Fixture"}))
    assert result["ok"] is False
    assert result["code"] == "containment_refused"
    assert kb.get_task(board, relay).status == "running"


def test_registered_cli_parser_and_tool_schema():
    from hermes_cli.kanban_parser import build_parser
    from tools.registry import registry
    from tools import kanban_tools  # executes normal registration
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="top"))
    args = parser.parse_args(["kanban", "contain", "t_0123456789abcdef", "--authority", "t_aabbccdd11223344", "--summary", "Fixture"])
    assert args.kanban_action == "contain"
    names = {entry[0] for entry in kanban_tools._TOOLS}
    assert {"kanban_contain", "kanban_resume_contained"} <= names
    assert registry is not None


def test_normal_connection_reopens_fence_and_released_attestation(board, monkeypatch):
    from hermes_cli import kanban_containment_service as service
    from hermes_cli import kanban_db_connect as kbc
    import sqlite3
    relay, authority, session = arguments(board, monkeypatch)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with scoped_current_session_id(session):
        result = service.contain(board, authority=authority, summary="Fixture")
    with kbc.connect_closing() as reopened:
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            reopened.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
        assert reopened.execute("PRAGMA database_list").fetchone()[2] == path
    task = kb.get_task(board, authority)
    for key, value in {"HERMES_KANBAN_TASK": authority, "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
                       "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock}.items():
        monkeypatch.setenv(key, value)
    kb._append_event(board, authority, "owner_admitted", {"session_id": session}, run_id=task.current_run_id)
    with scoped_current_session_id(session), kbc.connect_closing() as reopened:
        assert service.resume(reopened, task_id=relay, episode=result["episode"])["status"] == "ready"
    with kbc.connect_closing() as reopened:
        # A prior valid release does not re-hold every ordinary state update after reconnect.
        reopened.execute("UPDATE tasks SET status='todo' WHERE id=?", (relay,))


def test_unsynced_actor_bootstraps_missing_portable_columns(board, monkeypatch):
    from hermes_cli import kanban_containment_service as service
    from tests.hermes_cli import test_kanban_containment_legacy_paths as legacy
    relay, _ = legacy._relay(board)
    authority = kb.create_task(board, title="Fixture independent authority", assignee="default")
    task = kb.get_task(board, relay)
    session = "fixture-real-schema-owner"
    path = board.execute("PRAGMA database_list").fetchone()[2]
    for key, value in {"HERMES_KANBAN_TASK": relay, "HERMES_KANBAN_DB": path,
                       "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
                       "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock, "HERMES_SESSION_ID": session}.items():
        monkeypatch.setenv(key, value)
    kb._append_event(board, relay, "owner_admitted", {"session_id": session}, run_id=task.current_run_id)
    assert "uid" not in {r[1] for r in board.execute("PRAGMA table_info(task_runs)")}
    with scoped_current_session_id(session):
        result = service.contain(board, authority=authority, summary="Fixture native schema")
    assert result["status"] == "triage"
    assert board.execute("SELECT uid FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]


@pytest.mark.parametrize("key,value", [("authority", "bad"), ("producer", "bad"), ("task_id", "bad")])
def test_coordinates_are_validated_without_normalization(board, monkeypatch, key, value):
    from hermes_cli import kanban_containment_service as service
    from hermes_cli.kanban_containment_policy import ContainmentPolicyError
    relay, authority, session = arguments(board, monkeypatch)
    args = {"task_id": relay, "authority": authority, "summary": "Fixture", key: value}
    with scoped_current_session_id(session), pytest.raises(ContainmentPolicyError):
        service.contain(board, **args)
    assert kb.get_task(board, relay).status == "running"
