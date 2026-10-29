"""Restarts (session-outcome contract, lane A2): the backend boot sweep continues a chat a restart interrupted
even when no Desktop reopens it (on by default, existing freshness window), and the orphan reap tells a parent
chat when its subagent was killed mid-work. [daveey]"""

from __future__ import annotations

import json
import time

import pytest

import hermes_state_outcome as so
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway import startup_resume as sr

DEAD_PID = 2 ** 22 - 3  # never a live process


def _marker(home, key, age):
    path = home / "desktop" / "interrupted_turns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = {"prompt": "finish the migration", "started_at": time.time() - age, "attempts": 0,
                 "auto_continue": True, "writer_pid": DEAD_PID}
    path.write_text(json.dumps(data))


def test_boot_continues_a_fresh_unopened_chat_and_not_a_stale_one(monkeypatch):
    from hermes_cli import config
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    _marker(home, "fresh-chat", age=60)
    _marker(home, "stale-chat", age=3 * 3600)  # past the default 15 min window
    # Default config apart from the settle delay: resume_on_startup is not set anywhere.
    monkeypatch.setattr(config, "load_config", lambda: {"desktop": {"auto_continue": {
        "resume_on_startup_delay_seconds": 0}}})
    resumed = []
    monkeypatch.setitem(server._methods, "session.resume",
                        lambda rid, params: resumed.append(params["session_id"]) or {"result": {"auto_continue": {}}})

    thread = sr.start_startup_resume_sweep()

    assert thread is not None, "the boot sweep must run without resume_on_startup in config"
    thread.join(10)
    assert resumed == ["fresh-chat"]


@pytest.fixture
def db(tmp_path):
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def _age(db, sid, seconds):
    then = time.time() - seconds
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (then, sid))
    db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (then, sid))
    db._conn.commit()


@pytest.mark.parametrize("last_role, told", [("tool", True), ("assistant", False)])
def test_a_reaped_subagent_mid_work_tells_its_parent(monkeypatch, db, last_role, told):
    db.create_session("parent", source="desktop")
    db.append_message("parent", role="user", content="audit the three repos")
    db.append_message("parent", role="assistant", content="Delegated the audit to a subagent.")
    db.create_session("child", source="subagent", parent_session_id="parent")
    db._conn.execute("UPDATE sessions SET title = 'repo auditor' WHERE id = 'child'")
    db._conn.commit()
    db.append_message("child", role="user", content="audit repo A")
    if last_role == "tool":
        db.append_message("child", role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}])
        db.append_message("child", role="tool", content="ok", tool_call_id="c1", tool_name="terminal")
    else:
        db.append_message("child", role="assistant", content="Repo A is clean.")
    _age(db, "child", 8 * 3600)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_SESSION_TTL_S", 6 * 3600)

    assert "child" in server._sweep_orphaned_session_rows()

    notices = [m for m in db.get_messages("parent") if m.get("display_kind") == "kanban_notice"]
    if not told:
        assert notices == []
        return
    [notice] = notices
    assert "subagent repo auditor was stopped by a restart before finishing" in notice["content"]
    stored = so.get_outcome("parent", db=db)
    assert (stored["kind"], stored["reason"]) == ("unresolved", "subagent_killed")
