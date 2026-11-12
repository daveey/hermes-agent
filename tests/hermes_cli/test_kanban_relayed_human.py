"""kanban-d1b921f32e: a chat's relay of David's answer is verified before it can count as human. [daveey]"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_relayed_human as krh


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _state_db(home: Path, session: str, user_at: float) -> int:
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
               "role TEXT, content TEXT, timestamp REAL)")
    cur = db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'left ring', ?)",
                     (session, user_at))
    db.commit()
    db.close()
    return int(cur.lastrowid or 0)


def _asked(conn, at: int) -> str:
    tid = kb.create_task(conn, title="Pebble ring probe", assignee="default")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "blocked", {"kind": "needs_input", "reason": "which ring?"})
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'", (at, tid))
    return tid


def test_verify_accepts_one_newer_user_message_per_card(kanban_home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        mid = _state_db(kanban_home, "chat-1", now - 10)
        relay = krh.verify(conn, tid, session_id="chat-1")
        assert relay == {"session": "chat-1", "message_id": mid, "at": now - 10}
        kb.add_comment(conn, tid, "default", "David: left ring", writer_kind="agent", writer_session="chat-1",
                       relayed_human=relay)
        with pytest.raises(krh.RelayRefused, match="already relayed"):
            krh.verify(conn, tid, session_id="chat-1")


def test_verify_refuses_a_reply_older_than_the_ask(kanban_home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 10)
        _state_db(kanban_home, "chat-1", now - 100)
        with pytest.raises(krh.RelayRefused, match="predates"):
            krh.verify(conn, tid, session_id="chat-1")


def test_verify_refuses_workers_and_sessionless_callers(kanban_home, monkeypatch):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        _state_db(kanban_home, "chat-1", now - 10)
        with pytest.raises(krh.RelayRefused, match="session"):
            krh.verify(conn, tid, session_id=None)
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other")
        with pytest.raises(krh.RelayRefused, match="Kanban worker"):
            krh.verify(conn, tid, session_id="chat-1")


def test_relay_requires_agent_writer(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _asked(conn, int(time.time()) - 100)
        with pytest.raises(ValueError, match="relayed_human"):
            kb.add_comment(conn, tid, "daveey", "x", writer_kind="human",
                           relayed_human={"session": "s", "message_id": 1, "at": 1})


def _tool_comment(args: dict) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_comment(args))


def test_kanban_comment_tool_records_a_verified_relay(kanban_home, monkeypatch):
    now = int(time.time())
    monkeypatch.setenv("HERMES_SESSION_ID", "chat-1")
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
    _state_db(kanban_home, "chat-1", now - 10)
    out = _tool_comment({"task_id": tid, "body": "David says: left ring", "relays_user_message": True})
    assert out.get("ok") is True and out.get("relayed_human"), out
    with kbc.connect_closing() as conn:
        p = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'commented'",
                                    (tid,)).fetchone()[0])
    assert p["writer_kind"] == "agent" and p["relayed_human"]["session"] == "chat-1"
    refused = _tool_comment({"task_id": tid, "body": "and again", "relays_user_message": True})
    assert refused.get("ok") is not True and "already relayed" in json.dumps(refused)


def test_kanban_comment_tool_refuses_a_worker_relay(kanban_home, monkeypatch):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
    _state_db(kanban_home, "chat-1", now - 10)
    monkeypatch.setenv("HERMES_SESSION_ID", "chat-1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    out = _tool_comment({"task_id": tid, "body": "self-relay", "relays_user_message": True})
    assert out.get("ok") is not True and "Kanban worker" in json.dumps(out)


def test_remote_relay_is_stored_as_sent():
    assert krh.from_remote({"session": "hub-chat", "message_id": "7", "at": 5}) == {
        "session": "hub-chat", "message_id": 7, "at": 5, "via": "remote"}
    assert krh.from_remote({"session": "", "message_id": 7, "at": 5}) is None
    assert krh.from_remote("yes") is None
