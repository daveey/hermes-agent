"""A worker may archive an unclaimed card its own task created. [daveey]

The scoped-worker mutation gate refuses every other task. That left a creator
unable to delete a card kanban_create had just pinned local by mistake
("the scoped worker can't archive it"). Archive is the exception: creator_task_id
is the caller's task, and the card has not been claimed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


def _board(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    conn = connect(db)
    owner = kb.create_task(conn, title="owner")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_TASK", owner)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return conn, owner


def _created(conn, owner, title="misplaced"):
    return kb.create_task(conn, title=title, creator_task_id=owner)


def test_worker_tool_archives_an_unclaimed_card_it_created(tmp_path, monkeypatch):
    from tools import kanban_tools as kt

    conn, owner = _board(tmp_path, monkeypatch)
    child = _created(conn, owner)
    assert kb.get_task(conn, child).claim_lock is None
    out = json.loads(kt._handle_archive({"task_id": child}))
    assert out["ok"] and out["task_id"] == child, out
    assert kb.get_task(conn, child).status == "archived"
    conn.close()


def test_worker_tool_refuses_a_claimed_card_and_someone_elses(tmp_path, monkeypatch):
    from tools import kanban_tools as kt

    conn, owner = _board(tmp_path, monkeypatch)
    claimed = _created(conn, owner, "claimed")
    kb.claim_task(conn, claimed)
    foreign = kb.create_task(conn, title="foreign")
    claimed_out = json.loads(kt._handle_archive({"task_id": claimed}))
    foreign_out = json.loads(kt._handle_archive({"task_id": foreign}))
    assert claimed_out.get("ok") is not True and "claim" in claimed_out["error"].lower(), claimed_out
    assert foreign_out.get("ok") is not True and owner in foreign_out["error"], foreign_out
    assert kb.get_task(conn, claimed).status == "running"
    assert kb.get_task(conn, foreign).status == "ready"
    conn.close()


def test_worker_cli_archives_only_an_unclaimed_card_it_created(tmp_path, monkeypatch, capsys):
    from hermes_cli.kanban import _cmd_archive

    conn, owner = _board(tmp_path, monkeypatch)
    child = _created(conn, owner)
    foreign = kb.create_task(conn, title="foreign")
    assert _cmd_archive(SimpleNamespace(task_ids=[child], purge_ids=[])) == 0
    assert kb.get_task(conn, child).status == "archived"
    capsys.readouterr()
    assert _cmd_archive(SimpleNamespace(task_ids=[foreign], purge_ids=[])) != 0
    err = capsys.readouterr().err
    assert owner in err and "archive" in err.lower()
    assert kb.get_task(conn, foreign).status == "ready"
    conn.close()


def test_orchestrator_cli_can_still_archive_any_card(tmp_path, monkeypatch):
    from hermes_cli.kanban import _cmd_archive

    conn, _owner = _board(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    foreign = kb.create_task(conn, title="foreign")
    assert _cmd_archive(SimpleNamespace(task_ids=[foreign], purge_ids=[])) == 0
    assert kb.get_task(conn, foreign).status == "archived"
    conn.close()
