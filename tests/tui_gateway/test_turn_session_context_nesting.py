"""A queued model switch refreshes the stored system prompt on the TURN thread, after the turn
bound its session context. That refresh re-binds the same session and clears it in a
``finally``; the clear used to wipe the turn's binding, so every later tool call in the turn
saw no ``HERMES_SESSION_KEY``/``HERMES_SESSION_ID`` and ``kanban_create`` cards landed with
``session_id`` empty and no tui subscription (t_2cd33dfc). [daveey]"""

from gateway.session_context import _UNSET, _VAR_MAP, get_session_env
from tui_gateway import server


def _reset_vars():
    for var in _VAR_MAP.values():
        var.set(_UNSET)


def test_turn_session_context_survives_system_prompt_refresh(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    built = []

    class FakeAgent:
        session_id = "sess-1"
        _cached_system_prompt = None

        def _build_system_prompt(self, system_message=None):
            built.append(get_session_env("HERMES_SESSION_KEY"))
            return "prompt"

    class FakeDB:
        def update_system_prompt(self, key, prompt):
            pass

    agent = FakeAgent()
    agent._session_db = FakeDB()
    session = {"agent": agent, "session_key": "sess-1", "cwd": ""}
    sid = "ui-test-nesting"
    server._sessions[sid] = session
    try:
        tokens = server._set_session_context("sess-1", ui_session_id=sid)
        try:
            server._persist_live_session_system_prompt(session)
            assert built == ["sess-1"]
            # The turn's own binding is back for the rest of the turn.
            assert get_session_env("HERMES_SESSION_KEY") == "sess-1"
            assert get_session_env("HERMES_SESSION_ID") == "sess-1"
            assert get_session_env("HERMES_UI_SESSION_ID") == sid
        finally:
            server._clear_session_context(tokens)
        assert get_session_env("HERMES_SESSION_KEY") == ""
    finally:
        server._sessions.pop(sid, None)
        _reset_vars()


def test_kanban_create_target_resolves_after_refresh(monkeypatch):
    """End to end on the kanban side: the auto-subscribe target is still the turn's session."""
    from tools.kanban_tools import _resolve_notify_target

    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.setattr("tools.kanban_tools._live_tui_session_key", lambda key, profile: key)

    class FakeAgent:
        session_id = "sess-2"

        def _build_system_prompt(self, system_message=None):
            return "prompt"

    class FakeDB:
        def update_system_prompt(self, key, prompt):
            pass

    agent = FakeAgent()
    agent._session_db = FakeDB()
    session = {"agent": agent, "session_key": "sess-2", "cwd": ""}
    sid = "ui-test-nesting-2"
    server._sessions[sid] = session
    try:
        tokens = server._set_session_context("sess-2", ui_session_id=sid)
        try:
            server._persist_live_session_system_prompt(session)
            target = _resolve_notify_target()
            assert target is not None
            assert (target["platform"], target["chat_id"]) == ("tui", "sess-2")
        finally:
            server._clear_session_context(tokens)
    finally:
        server._sessions.pop(sid, None)
        _reset_vars()
