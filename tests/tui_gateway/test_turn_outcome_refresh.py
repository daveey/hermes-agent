"""Desktop/TUI call sites of the stored chat outcome: every prompt-turn conclusion refreshes it
(complete and raising turns alike), and the startup orphan reap refreshes each reaped row."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import hermes_state_outcome as so
from hermes_state import SessionDB
from tui_gateway import server

SUMMARY = "Rotated the API key, redeployed the worker and confirmed the health check is green."


class _ImmediateThread:
    def __init__(self, target=None, daemon=None, **_kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


class _Agent:
    model = "test-model"
    provider = "test-provider"

    def __init__(self, session_id: str, db: SessionDB, reply: str | None):
        self.session_id = session_id
        self._db = db
        self._reply = reply

    def clear_interrupt(self):
        return None

    def run_conversation(self, prompt, conversation_history=None, stream_callback=None, **_kwargs):
        self._db.append_message(self.session_id, role="user", content=prompt)
        if self._reply is None:
            raise RuntimeError("provider exploded")
        self._db.append_message(self.session_id, role="assistant", content=self._reply)
        return {"final_response": self._reply, "messages": []}


def _session(agent: _Agent) -> dict:
    return {
        "agent": agent, "session_key": agent.session_id, "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
        "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
    }


@pytest.fixture
def db(tmp_path: Path):
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


@pytest.fixture
def turn_env(monkeypatch, tmp_path, db):
    for name, stub in {
        "_emit": lambda *_a, **_k: None, "make_stream_renderer": lambda _cols: None,
        "render_message": lambda _raw, _cols: None, "_wire_callbacks": lambda _sid: None,
        "_sync_agent_model_with_config": lambda *_a: None, "_session_cwd": lambda _session: str(tmp_path),
        "_register_session_cwd": lambda _session: None, "_set_session_context": lambda *_a, **_k: [],
        "_clear_session_context": lambda _tokens: None, "_session_info": lambda *_a: {},
        "_get_usage": lambda _agent: {}, "_sync_session_key_after_compress": lambda *_a, **_k: None,
        "_drain_queued_prompt": lambda *_a: False, "_voice_tts_enabled": lambda: False, "_get_db": lambda: db,
    }.items():
        monkeypatch.setattr(server, name, stub)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
    from tui_gateway import session_workdir
    monkeypatch.setattr(session_workdir, "_get_db", lambda: db, raising=False)
    yield monkeypatch
    for sid in [s for s in server._sessions if s.startswith("ui-outcome-")]:
        server._sessions.pop(sid, None)


@pytest.mark.parametrize("reply, kind, reason", [
    (SUMMARY, "done", None), ("Should I also rotate the staging key?", "unresolved", "no_summary"),
    (None, "unresolved", "interrupted"),
])
def test_every_turn_conclusion_refreshes_the_stored_outcome(turn_env, db, reply, kind, reason):
    db.create_session("row-o", source="desktop")
    agent = _Agent("row-o", db, reply)
    session = server._sessions["ui-outcome-1"] = _session(agent)

    assert server._run_prompt_submit("rid", "ui-outcome-1", session, "rotate the key") is True

    stored = so.get_outcome("row-o", db=db)
    assert stored is not None, "the turn end must store an outcome"
    assert (stored["kind"], stored.get("reason")) == (kind, reason)


def test_orphan_reap_refreshes_each_reaped_row(monkeypatch, db):
    db.create_session("reaped", source="desktop")
    db.append_message("reaped", role="user", content="long task")
    stale = time.time() - 8 * 3600
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (stale, "reaped"))
    db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (stale, "reaped"))
    db._conn.commit()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_SESSION_TTL_S", 6 * 3600)

    assert server._sweep_orphaned_session_rows() == ["reaped"]

    stored = so.get_outcome("reaped", db=db)
    assert (stored["kind"], stored["reason"]) == ("unresolved", "interrupted")
