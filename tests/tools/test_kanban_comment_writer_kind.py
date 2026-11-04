"""Comment writer provenance: who wrote a card note, an agent or a human. [daveey]

Regression for self-heal agent-261aa31f49: a chat agent's ``kanban_comment`` ("Please re-check (c)
yourself, then complete.") reached the worker as ``New note on your kanban task from the operator`` /
``default (another session)``, the same stamp a human's note gets, so the ask-gate scored it as David
saying "do it yourself". Every actor shares the ``default`` profile, so the author name can't tell them
apart; the ``commented`` event now records ``writer_kind`` and the live note says when an agent wrote it.
"""
from __future__ import annotations

import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
import tools.kanban_tools as kt
from tests.tools import test_kanban_comment_injection as inj

worker_home = inj.worker_home
FakeAgent = inj.FakeAgent
AGENT_HEADER = "on your kanban task from another agent session (delivered mid-run)"
OPERATOR_HEADER = "on your kanban task from the operator (delivered mid-run)"


def _payload(tid: str, cid: int) -> dict:
    conn = kbc.connect()
    try:
        for e in kb.list_events(conn, tid):
            if e.kind == "commented" and (e.payload or {}).get("comment_id") == cid:
                return e.payload
    finally:
        conn.close()
    raise AssertionError(f"no commented event for comment {cid}")


def _ready_task(title="t") -> str:
    conn = kbc.connect()
    try:
        return kb.create_task(conn, title=title)
    finally:
        conn.close()


def test_tool_comment_event_records_agent_writer(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    tid = _ready_task()
    out = json.loads(kt._handle_comment({"task_id": tid, "body": "Please re-check (c) yourself"}))
    assert _payload(tid, out["comment_id"])["writer_kind"] == "agent"


def test_tool_comment_records_the_writing_session(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from gateway.session_context import clear_session_vars, set_session_vars
    tid = _ready_task()
    tokens = set_session_vars(session_id="sess-chat-1")
    try:
        out = json.loads(kt._handle_comment({"task_id": tid, "body": "note"}))
    finally:
        clear_session_vars(tokens)
    assert _payload(tid, out["comment_id"])["writer_session"] == "sess-chat-1"


def test_cli_comment_event_records_human_writer(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban
    tid = _ready_task()
    assert "Comment added" in kanban.run_slash(f"comment {tid} why are you asking me?")
    conn = kbc.connect()
    try:
        cid = kb.list_comments(conn, tid)[-1].id
    finally:
        conn.close()
    assert _payload(tid, cid)["writer_kind"] == "human"


def test_dashboard_comment_event_records_human_writer(worker_home, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "kanban_dash_api_wk", Path(kt.__file__).resolve().parents[1] / "plugins/kanban/dashboard/plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    import sys
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router)
    tid = _ready_task()
    r = TestClient(app).post(f"/tasks/{tid}/comments", json={"body": "do it yourself"})
    assert r.status_code == 200, r.text
    conn = kbc.connect()
    try:
        cid = kb.list_comments(conn, tid)[-1].id
    finally:
        conn.close()
    assert _payload(tid, cid)["writer_kind"] == "human"


def test_unknown_writer_kind_is_rejected(worker_home):
    tid = _ready_task()
    conn = kbc.connect()
    try:
        with pytest.raises(ValueError):
            kb.add_comment(conn, tid, "default", "x", writer_kind="robot")
        cid = kb.add_comment(conn, tid, "kanban-dispatcher", "system note")
    finally:
        conn.close()
    assert "writer_kind" not in _payload(tid, cid)  # system writers: unchanged payload


def _chat_tool_comment(tid: str, body: str) -> None:
    """A Desktop chat on the same profile posting via the kanban_comment tool (no worker env)."""
    inj._as_other_session(lambda: kt._handle_comment({"task_id": tid, "body": body}))


def test_agent_note_reaches_worker_with_agent_header_and_stamp(worker_home, monkeypatch):
    tid, _run = inj._running_worker(monkeypatch)
    agent = FakeAgent()
    inj._unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False  # seed
    _chat_tool_comment(tid, "Please re-check (c) yourself, then complete.")
    inj._unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    note = agent.steers[0]
    assert note.startswith("New note " + AGENT_HEADER), note
    assert "- default (agent chat, another session) at " in note
    assert "from the operator" not in note


def test_mixed_notes_keep_operator_header_and_stamp_each_line(worker_home, monkeypatch):
    tid, _run = inj._running_worker(monkeypatch)
    agent = FakeAgent()
    inj._unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed
    _chat_tool_comment(tid, "agent says: re-check it yourself")
    from hermes_cli import kanban
    inj._as_other_session(lambda: kanban.run_slash(f"comment {tid} why are you asking me?"))
    inj._unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    note = agent.steers[0]
    assert note.startswith("New notes " + OPERATOR_HEADER), note
    assert "- default (agent chat, another session) at " in note
    assert "- default (another session) at " in note  # the human line keeps the old stamp
