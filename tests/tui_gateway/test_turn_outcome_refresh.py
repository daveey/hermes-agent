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
    yield monkeypatch
    for sid in [s for s in server._sessions if s.startswith(("ui-outcome-", "ui-retry"))]:
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


def test_a_later_good_reply_clears_stored_turn_failed_without_manual_refresh(turn_env, db):
    """Hub chats 20261120_041627_3ea0c6 / d40002 / 2e950a: a failed-turn row then a later
    successful TUI reply must not leave ``unresolved{turn_failed}`` until ``hermes sessions
    outcome --refresh``. The second ``_run_prompt_submit`` finally is the turn_end refresh."""
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE

    db.create_session("row-retry", source="desktop")
    db.append_message("row-retry", role="user", content="archive my receipts")
    db.append_message(
        "row-retry", role="assistant", content=FAILED_TURN_NOTICE, display_kind=FAILED_TURN_DISPLAY_KIND,
    )
    # The fixture's ImmediateThread cannot run ``Thread(target=fn, args=...)`` workers the
    # kanban-card reader starts; an unread board must not decide this test.
    turn_env.setattr(so, "_live_work", lambda db, sid, ids: ([], [], False))
    assert so.refresh_outcome("row-retry", "turn_end", db=db)["reason"] == "turn_failed"
    assert so.get_outcome("row-retry", db=db)["reason"] == "turn_failed"

    agent = _Agent("row-retry", db, SUMMARY)
    session = server._sessions["ui-retry"] = _session(agent)
    assert server._run_prompt_submit("rid", "ui-retry", session, "try again") is True

    stored = so.get_outcome("row-retry", db=db)
    assert stored is not None, "the successful turn end must store an outcome"
    assert stored["kind"] == "done", stored


def test_closed_session_refresh_clears_stored_turn_failed_after_a_later_good_reply(monkeypatch, db):
    """Hub 3ea0c6: later complete turn, then ws_orphan_reap, stored still turn_failed until
    ``hermes sessions outcome --refresh``. In-memory reap must refresh without that CLI."""
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE
    from tui_gateway.turn_outcome import concluding_turn_identity, refresh_closed_session_outcome

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(so, "_live_work", lambda db, sid, ids: ([], [], False))
    db.create_session("reaped-retry", source="desktop")
    db.append_message("reaped-retry", role="user", content="archive my receipts")
    db.append_message(
        "reaped-retry", role="assistant", content=FAILED_TURN_NOTICE, display_kind=FAILED_TURN_DISPLAY_KIND,
    )
    assert so.refresh_outcome("reaped-retry", "turn_end", db=db)["reason"] == "turn_failed"
    db.append_message("reaped-retry", role="assistant", content=SUMMARY)
    assert so.get_outcome("reaped-retry", db=db)["reason"] == "turn_failed"

    only_active = type("A", (), {"_active_session_turn_lease_holder": "lease-1"})()
    assert "lease:lease-1" in concluding_turn_identity(only_active)

    refresh_closed_session_outcome({"session_key": "reaped-retry"})
    stored = so.get_outcome("reaped-retry", db=db)
    assert stored is not None and stored["kind"] == "done", stored


def test_ws_orphan_teardown_refreshes_closed_session_outcome(monkeypatch):
    from tui_gateway import turn_outcome as to

    seen: list[str] = []
    monkeypatch.setattr(to, "refresh_closed_session_outcome", lambda s: seen.append(s.get("session_key") or ""))
    monkeypatch.setattr(server, "_teardown_session", lambda *a, **k: None)
    monkeypatch.setattr("tui_gateway.detached_turn_cleanup.cancel", lambda s: None)
    monkeypatch.setattr("tui_gateway.event_replay.forget_session", lambda s: None)
    session = {"session_key": "reaped-retry", "history_lock": threading.Lock(), "_sid": "ui-reap"}
    assert server._teardown_popped_session(session, end_reason="ws_orphan_reap") is True
    assert seen == ["reaped-retry"]
    seen.clear()
    assert server._teardown_popped_session(session, end_reason="tui_close") is True
    assert seen == []
    assert server._teardown_popped_session(session, end_reason="tui_shutdown") is True
    assert seen == []


def test_refresh_uses_server_bound_session_db(monkeypatch, db, caplog):
    """Only ``server._get_db`` is stubbed, as in production: session_workdir's raw module copy has no ``_get_db``
    (it is rebound onto server globals), so the refresh must reach ``_session_db`` through ``server``."""
    import logging

    from tui_gateway import session_workdir
    from tui_gateway.turn_outcome import refresh_turn_outcome

    assert not hasattr(session_workdir, "_get_db"), "the split module must not carry server's _get_db"
    monkeypatch.setattr(server, "_get_db", lambda: db)
    db.create_session("row-bound", source="desktop")
    db.append_message("row-bound", role="user", content="rotate the key")
    db.append_message("row-bound", role="assistant", content=SUMMARY)

    with caplog.at_level(logging.WARNING, logger="tui_gateway.turn_outcome"):
        refresh_turn_outcome({"session_key": "row-bound"}, agent=None)

    assert not [r for r in caplog.records if "turn-end outcome refresh failed" in r.getMessage()]
    stored = so.get_outcome("row-bound", db=db)
    assert stored is not None and stored["kind"] == "done"


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


# ---- agent-init failure ("failed before the agent was ready") --------------------------------------------
# The deferred build failed (ModelRoutingError and the like), so no agent ever ran the turn: neither the
# conversation-loop closer nor the gateway closer exists on this path. The TUI turn thread itself must close
# the accepted user row with the failed-turn boundary and refresh the stored outcome, else the chat reads
# unresolved{interrupted} forever.

def _init_failed_session(sid: str, *, error: str | None = "ModelRoutingError: No eligible deep routes") -> dict:
    ready = threading.Event()
    ready.set()
    return {
        "agent": None, "agent_ready": ready, "agent_error": error, "session_key": sid, "history": [],
        "history_lock": threading.Lock(), "history_version": 0, "running": True, "cols": 80, "inflight_turn": None,
    }


@pytest.fixture
def init_env(monkeypatch, db):
    for name, stub in {"_emit": lambda *_a, **_k: None, "render_message": lambda _raw, _cols: None,
                       "_session_info": lambda *_a: {}, "_get_db": lambda: db}.items():
        monkeypatch.setattr(server, name, stub)
    return monkeypatch


def _failed_rows(db, sid):
    return [m for m in db.get_messages(sid) if m.get("display_kind") == "failed_turn"]


def test_agent_init_failure_writes_one_boundary_and_reads_turn_failed(init_env, db):
    from agent.turn_failure_copy import FAILED_TURN_NOTICE

    db.create_session("row-init", source="desktop")
    db.append_message("row-init", role="user", content="rotate the key")
    session = _init_failed_session("row-init")

    server._run_after_agent_ready("rid", "ui-init-1", session, "rotate the key", None, None, None)

    rows = _failed_rows(db, "row-init")
    assert len(rows) == 1, rows
    boundary = rows[0]
    assert boundary["role"] == "assistant" and boundary["content"] == FAILED_TURN_NOTICE
    meta = boundary.get("display_metadata") or {}
    assert meta.get("error_surface") == {"layer": "runtime", "code": "agent_init_failed", "retryable": True}
    assert "No eligible deep routes" in meta.get("error", "")
    assert db.latest_conversation_role("row-init") == "assistant"
    stored = so.get_outcome("row-init", db=db)
    assert stored is not None and (stored["kind"], stored.get("reason")) == ("unresolved", "turn_failed")
    assert session["running"] is False


def test_agent_init_failure_redelivery_adds_no_second_boundary(init_env, db):
    db.create_session("row-init2", source="desktop")
    db.append_message("row-init2", role="user", content="rotate the key")

    for _ in range(2):
        server._run_after_agent_ready(
            "rid", "ui-init-2", _init_failed_session("row-init2"), "rotate the key", None, None, None)

    assert len(_failed_rows(db, "row-init2")) == 1
    stored = so.get_outcome("row-init2", db=db)
    assert (stored["kind"], stored.get("reason")) == ("unresolved", "turn_failed")


def test_agent_init_failure_secret_in_error_is_redacted(init_env, db):
    db.create_session("row-init3", source="desktop")
    db.append_message("row-init3", role="user", content="hi")
    secret = "sk-ant-api03-" + "A" * 40
    server._run_after_agent_ready(
        "rid", "ui-init-3", _init_failed_session("row-init3", error=f"bad key {secret}"), "hi", None, None, None)

    (row,) = _failed_rows(db, "row-init3")
    assert secret not in str(row.get("display_metadata"))


def test_cancelled_before_ready_turn_is_unchanged(init_env, db):
    """Control: a turn cancelled before the build finished writes no boundary (the user stopped it)."""
    db.create_session("row-cancel", source="desktop")
    db.append_message("row-cancel", role="user", content="rotate the key")
    session = _init_failed_session("row-cancel", error=None)
    session["_turn_cancel_requested"] = True

    server._run_after_agent_ready("rid", "ui-cancel", session, "rotate the key", None, None, None)

    assert _failed_rows(db, "row-cancel") == []
    assert db.latest_conversation_role("row-cancel") == "user"
    assert so.get_outcome("row-cancel", db=db) is None
