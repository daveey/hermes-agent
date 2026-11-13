"""Who wrote this turn? ``prompt.submit`` stamps ``agent._input_origin`` for ``pre_llm_call`` plugins. [daveey]

The coach complaint lane (hermes-mods) filed defects from agent-driven Desktop proof chats: an agent script
dialing ``/api/ws?client=agent`` reads to a plugin exactly like David typing. ``human_input.input_origin``
classifies each client submit as ``"agent"``, ``"human"`` or ``""`` (unknown), ``prompt.submit`` carries it to
the turn, and ``_collect_pre_llm_call_context`` passes it as the ``input_origin`` hook kwarg. Injected turns
(Kanban notices, auto-continue) run ``_run_prompt_submit`` directly and carry ``""``.
"""

from __future__ import annotations

import threading
import types

import pytest

from tests.tui_gateway import test_prompt_admission as admission_tests
from tui_gateway import human_input, server


class _Transport:
    """A stub WS transport (hashable: a session keys its viewers by transport)."""

    auth_identity = None
    _peer = "t"

    def __init__(self, client_kind=None):
        self.client_kind = client_kind

    def write(self, obj):
        return True

    def close(self):
        return None


def _transport(client_kind=None):
    return _Transport(client_kind)


def _bound(client_kind=None):
    from tui_gateway.transport import bind_transport

    return bind_transport(_transport(client_kind))


# ── the classifier ───────────────────────────────────────────────────────────────────────────────────────


def test_input_origin_human_for_a_typed_desktop_submit():
    assert human_input.input_origin("r", {"text": "hi"}, "hi") == "human"
    assert human_input.input_origin("r", {"text": "hi"}, "hi", session={"agent_created": False}) == "human"


def test_input_origin_agent_for_an_agent_transport():
    from tui_gateway.transport import reset_transport

    assert human_input.input_origin("r", {}, "hi", transport=_transport("agent")) == "agent"
    token = _bound("agent")
    try:
        assert human_input.input_origin("r", {}, "hi") == "agent", "defaults to the bound transport"
    finally:
        reset_transport(token)
    assert human_input.input_origin("r", {}, "hi", transport=_transport("renderer")) == "human"


@pytest.mark.parametrize("session", [{"agent_created": True}, {"pending_hidden": True}])
def test_input_origin_agent_for_agent_created_or_hidden_sessions(session):
    assert human_input.input_origin("r", {}, "hi", session=session) == "agent"


@pytest.mark.parametrize("params", [
    {"_turn_author": object()}, {"_hosted_task": {}}, {"_hosted_terminal_callback": print},
    {"display_kind": "hidden"}])
def test_input_origin_agent_for_relayed_hosted_and_hidden_submits(params):
    assert human_input.input_origin("r", params, "hi") == "agent"


def test_input_origin_agent_for_a_cross_session_send():
    text = human_input.CROSS_SESSION_HEADER + ' "planner" (@session:default/abc)]\n\nplease check X'
    assert human_input.input_origin("r", {}, "  " + text) == "agent"


@pytest.mark.parametrize("rid,params,text", [
    (None, {}, "start chat"),  # start_chat's in-process submit
    (None, {"display_kind": "hidden"}, "widget"),  # in-process: unknown, whatever its params
    ("r", {}, "   "),
    ("r", {}, None),
    ("r", None, "hi"),
])
def test_input_origin_unknown_otherwise(rid, params, text):
    assert human_input.input_origin(rid, params, text) == ""


def test_input_origin_never_raises(monkeypatch):
    import tui_gateway.transport as transport_mod

    def boom(t):
        raise RuntimeError("broken transport")

    monkeypatch.setattr(transport_mod, "is_agent_transport", boom)
    assert human_input.input_origin("r", {}, "hi", transport=_transport()) == ""


def test_note_submit_returns_the_origin_and_stamps_only_humans(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    stamps = []
    monkeypatch.setattr(human_input, "record_human_input", lambda sid="", home=None: stamps.append(sid))
    assert human_input.note_submit("r", {"session_id": "a"}, "hi", transport=_transport("agent")) == "agent"
    assert human_input.note_submit("r", {"session_id": "b"}, "hi", session={"pending_hidden": True}) == "agent"
    assert human_input.note_submit(None, {"session_id": "c"}, "hi") == ""
    assert human_input.note_submit("r", {"session_id": "d"}, "hi", transport=_transport()) == "human"
    assert stamps == ["d"]


# ── prompt.submit carries the origin onto the agent for the turn ─────────────────────────────────────────


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _session(agent, **extra):
    ready = threading.Event()
    ready.set()
    return {"agent": agent, "agent_ready": ready, "agent_error": None, "session_key": "session-key",
            "history": [], "history_lock": threading.Lock(), "history_version": 0, "running": False,
            "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None, "show_reasoning": False,
            "tool_progress_mode": "all", "inflight_turn": None, "transport": None, **extra}


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    from hermes_cli import kanban_session_activity as activity
    monkeypatch.setattr(activity, "_refresh_links_in_background", lambda scope: None)
    for name in ("_emit", "_wire_callbacks", "_sync_agent_model_with_config", "_register_session_cwd",
                 "_tts_stream_begin", "_sync_session_key_after_compress"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session: None)
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a, **k: True)
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: fn())
    seen: list = []

    def make_agent():
        agent = types.SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None)
        agent.run_conversation = lambda message, **kw: (
            seen.append(getattr(agent, "_input_origin", "<unset>")) or {"final_response": "ok"})
        return agent

    return make_agent, seen


def _submit_on(client_kind, session, text="prove the fix"):
    from tui_gateway.transport import reset_transport

    server._sessions["origin-sid"] = session
    token = _bound(client_kind)
    try:
        return server._methods["prompt.submit"]("r1", {"session_id": "origin-sid", "text": text})
    finally:
        reset_transport(token)
        server._sessions.pop("origin-sid", None)


def test_prompt_submit_over_an_agent_transport_runs_with_origin_agent(turn_env):
    make_agent, seen = turn_env
    resp = _submit_on("agent", _session(make_agent()))
    assert resp.get("result", {}).get("status") == "streaming", resp
    assert seen == ["agent"]


def test_prompt_submit_into_an_agent_created_session_runs_with_origin_agent(turn_env):
    make_agent, seen = turn_env
    _submit_on(None, _session(make_agent(), agent_created=True))
    assert seen == ["agent"]


def test_prompt_submit_typed_on_desktop_runs_with_origin_human(turn_env):
    make_agent, seen = turn_env
    _submit_on(None, _session(make_agent()))
    assert seen == ["human"]


def test_injected_turn_resets_a_previous_origin(turn_env):
    make_agent, seen = turn_env
    agent = make_agent()
    session = _session(agent)
    _submit_on(None, session)
    session["running"] = True
    server._run_prompt_submit("rid", "origin-sid", session, "[injected] card done", display_kind="kanban_notice")
    assert seen == ["human", ""], "an injected turn must not inherit the last typed turn's origin"


def test_prompt_submit_consumes_the_stashed_origin(turn_env):
    make_agent, seen = turn_env
    session = _session(make_agent())
    _submit_on("agent", session)
    assert seen == ["agent"] and "_submit_input_origin" not in session, "read once, never left for a later turn"


# ── prompt.admit (in-process owner services) never inherits a client submit's origin ─────────────────────

owner = admission_tests.owner


def test_admitted_prompt_drops_a_stale_client_origin(owner, monkeypatch):
    _db, session, params, started = owner
    session["_submit_input_origin"] = "human"  # e.g. left by a client turn whose dispatch never started
    assert "result" in admission_tests.call("prompt.admit", params)
    seen = []
    monkeypatch.setattr(server, "_run_after_agent_ready",
                        lambda *a, **kw: seen.append(session.get("_submit_input_origin", "")))
    started[0]()
    assert seen == [""]
