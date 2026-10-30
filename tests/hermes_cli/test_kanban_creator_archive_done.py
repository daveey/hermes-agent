"""A worker may archive a DONE card its own task created. [daveey]

A scratch waiter a worker creates for a live proof is claimed when it wakes and completed
by its own worker; after that the creator could never archive it (the creator rule only
allowed unclaimed cards), so every such waiter leaked onto the board. A done card with no
live claim and no open child is cleanup, like a done capability owner card.

Still refused: a running/claimed card, a done card an open card waits on, a card another
task created, and the worker's own task.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

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


def _done(conn, creator, title="waiter"):
    tid = kb.create_task(conn, title=title, creator_task_id=creator)
    assert kb.claim_task(conn, tid) is not None
    assert kb.complete_task(conn, tid, summary="proof done", force=True)
    task = kb.get_task(conn, tid)
    assert task.status == "done" and not task.claim_lock and not task.worker_pid, task
    return tid


def _archive(tid):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_archive({"task_id": tid}))


def test_worker_archives_its_own_done_scratch_card(tmp_path, monkeypatch):
    conn, owner = _board(tmp_path, monkeypatch)
    waiter = _done(conn, owner)
    out = _archive(waiter)
    assert out.get("ok") and out["task_id"] == waiter, out
    assert kb.get_task(conn, waiter).status == "archived"
    conn.close()


def test_worker_cli_archives_its_own_done_card(tmp_path, monkeypatch):
    from hermes_cli.kanban import _cmd_archive

    conn, owner = _board(tmp_path, monkeypatch)
    waiter = _done(conn, owner)
    assert _cmd_archive(SimpleNamespace(task_ids=[waiter], purge_ids=[])) == 0
    assert kb.get_task(conn, waiter).status == "archived"
    conn.close()


def test_worker_still_refused_its_own_running_card(tmp_path, monkeypatch):
    conn, owner = _board(tmp_path, monkeypatch)
    running = kb.create_task(conn, title="running", creator_task_id=owner)
    kb.claim_task(conn, running)
    out = _archive(running)
    assert out.get("ok") is not True and "claim" in out["error"].lower(), out
    assert kb.get_task(conn, running).status == "running"
    conn.close()


def test_worker_still_refused_its_own_done_card_with_an_open_child(tmp_path, monkeypatch):
    conn, owner = _board(tmp_path, monkeypatch)
    waiter = _done(conn, owner)
    child = kb.create_task(conn, title="follow-up", parents=[waiter], creator_task_id=owner)
    assert kb.get_task(conn, child).status not in ("done", "archived")
    out = _archive(waiter)
    assert out.get("ok") is not True and child in out["error"], out
    assert kb.get_task(conn, waiter).status == "done"
    conn.close()


def test_worker_archives_its_own_done_card_once_its_child_is_done(tmp_path, monkeypatch):
    conn, owner = _board(tmp_path, monkeypatch)
    waiter = _done(conn, owner)
    child = kb.create_task(conn, title="follow-up", parents=[waiter], creator_task_id=owner)
    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, summary="ok", force=True)
    out = _archive(waiter)
    assert out.get("ok") and out["task_id"] == waiter, out
    conn.close()


def test_worker_still_refused_a_foreign_done_card(tmp_path, monkeypatch):
    conn, owner = _board(tmp_path, monkeypatch)
    other = kb.create_task(conn, title="other owner")
    foreign = _done(conn, other, "foreign waiter")
    out = _archive(foreign)
    assert out.get("ok") is not True and owner in out["error"], out
    assert kb.get_task(conn, foreign).status == "done"
    conn.close()


def test_worker_still_refused_its_own_task(tmp_path, monkeypatch):
    conn, owner = _board(tmp_path, monkeypatch)
    out = _archive(owner)
    assert out.get("ok") is not True and "assigned task" in out["error"], out
    assert kb.get_task(conn, owner).status != "archived"
    conn.close()
