"""Origin visibility is the chat projection, not a replica link receipt."""
import argparse
import json
import sqlite3

import pytest

from hermes_cli import kanban, kanban_session_activity as activity
from tests.hermes_cli import test_kanban_session_activity as helpers

kanban_root = helpers.kanban_root


def run(*argv):
    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers(dest="command"))
    return kanban.kanban_command(parser.parse_args(["kanban", "chat-tasks", *argv]))


@pytest.fixture
def lineage(monkeypatch):
    from hermes_cli import kanban_chat_tasks
    monkeypatch.setattr(kanban_chat_tasks, "lineage", lambda sid: [sid])


def test_unlinked_card_is_not_visible(kanban_root, lineage, capsys):
    helpers._make_board(kanban_root / "kanban.db", [("t_hidden", "ready", "other")])
    assert run("chat", "--expect", "t_hidden", "--json") == 4
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize("status", ["ready", "done", "archived"])
def test_linked_card_visible(kanban_root, lineage, capsys, status):
    helpers._make_board(kanban_root / "kanban.db", [("t_linked", status, "chat")])
    assert run("chat", "--expect", "default/t_linked", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "t_linked"


@pytest.mark.parametrize("expected,code", [("t1", 0), ("work/t1", 0), ("remote/work/t1", 0),
                                           ("wrong/work/t1", 4), ("remote/other/t1", 4)])
def test_qualified_identity(monkeypatch, lineage, expected, code):
    monkeypatch.setattr(activity, "session_tasks", lambda *a, **kw: [
        {"id": "t1", "key": "remote/work/t1", "board": "work", "gateway": "remote"}])
    assert run("chat", "--expect", expected) == code


def test_projection_read_error(monkeypatch, lineage, capsys):
    def fail(*a, **kw):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(activity, "session_tasks", fail)
    assert run("chat", "--expect", "t1", "--json") == 1
    assert "unavailable" in capsys.readouterr().err


def test_real_lineage(kanban_root, monkeypatch, capsys):
    from hermes_state import SessionDB
    monkeypatch.setenv("HERMES_HOME", str(kanban_root))
    db = SessionDB(kanban_root / "state.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session("tip", source="cli", parent_session_id="root")
    finally:
        db.close()
    helpers._make_board(kanban_root / "kanban.db", [("t1", "done", "root")])
    assert run("tip", "--expect", "t1", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "t1"


@pytest.mark.parametrize("expected,code", [("local/default/t1", 0), ("wrong/default/t1", 4),
                                           ("other/t1", 4), ("t_other", 4)])
def test_local_qualified_identity(monkeypatch, lineage, expected, code):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("local", set()))
    monkeypatch.setattr(activity, "session_tasks", lambda *a, **kw: [
        {"id": "t1", "key": "default/t1", "board": "default"}])
    assert run("chat", "--expect", expected) == code


def test_no_expect_and_text_output(monkeypatch, lineage, capsys):
    monkeypatch.setattr(activity, "session_tasks", lambda *a, **kw: [
        {"id": "t1", "key": "default/t1", "status": "ready", "title": "Visible"}])
    assert run("chat") == 0
    assert capsys.readouterr().out == "default/t1  ready  Visible\n"


def test_lineage_error_is_not_absence(monkeypatch, capsys):
    from hermes_cli import kanban_chat_tasks
    def fail(sid):
        raise OSError("session store unavailable")
    monkeypatch.setattr(kanban_chat_tasks, "lineage", fail)
    assert run("chat", "--json") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "session store unavailable" in captured.err


@pytest.mark.parametrize("expected,code", [("remote/work/t1", 0), ("remote/research/work/t1", 0),
                                           ("wrong/work/t1", 4), ("remote/other/t1", 4),
                                           ("remote/wrong/work/t1", 4)])
def test_real_profile_qualified_projection(monkeypatch, lineage, expected, code):
    from hermes_cli.kanban_replica_projection import card_rows
    projected = card_rows(("remote", "research", "work", "t1"), {"status": "ready"},
                          sids=["chat"], closed=False, synced=1, stale=False, me="local",
                          origin_profile="default", legacy_record=False, historical={})
    monkeypatch.setattr(activity, "session_tasks", lambda *a, **kw: [row for _, row in projected])
    assert run("chat", "--expect", expected) == code


def test_named_origin_profile_in_command(monkeypatch):
    from hermes_cli import kanban_origin, kanban_db_notify
    monkeypatch.setattr(kanban_db_notify, "add_notify_sub", lambda *a, **kw: None)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("hub", set()))
    receipt = kanban_origin.bind(None, "t1", kanban_origin.Origin("mac", "research", "chat"))
    assert receipt["verify_visible"] == "hermes -p research kanban chat-tasks chat --expect hub/default/t1"


def test_tool_guidance_names_projection():
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA
    text = json.dumps(KANBAN_CREATE_SCHEMA)
    assert "origin_binding.verify_visible" in text
    assert "not visibility proof" in text
