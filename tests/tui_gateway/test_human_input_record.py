"""Typed prompts stamp ``<HERMES_HOME>/desktop/last_human_input.json``; injected turns never do. [daveey]

hermes-sync opens a core-update drain window (new messages wait up to 15 min) only while the user is
away. A user typing into a MacBook-hosted chat from another Mac's Desktop (desktop-ssh) leaves this Mac's
HID idle time high, so the serve backend records every human-typed ``prompt.submit`` (t_01a1825a82e472f6).
Kanban notices, auto-continue resumes and cross-session sends must not count as the user being active.
"""

from __future__ import annotations

import json
import threading
import time
import types

import pytest
pytestmark = pytest.mark.usefixtures("no_background_link_refresh")

from tui_gateway import human_input, server


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    return tmp_path


def _stamp(home):
    path = home / "desktop" / "last_human_input.json"
    return json.loads(path.read_text()) if path.exists() else None


def _submit(params, rid="r1"):
    return server._methods["prompt.submit"](rid, params)


def test_typed_prompt_submit_records_the_time(home):
    before = time.time()
    _submit({"session_id": "no-such-session", "text": "hello there"})
    stamp = _stamp(home)
    assert stamp is not None, "a typed prompt.submit must stamp last_human_input.json"
    assert stamp["at"] >= before and stamp["session_id"] == "no-such-session"
    assert not list((home / "desktop").glob(".*.tmp")), "written atomically (no temp file left)"


def test_queued_composer_drain_still_counts(home):
    _submit({"session_id": "s", "text": "run this after", "queued": True})
    assert _stamp(home) is not None


def test_cross_session_send_does_not_record(home):
    text = '[Message from another Hermes session "planner" (@session:default/abc)]\n\nplease check X'
    _submit({"session_id": "s", "text": text, "queued": True})
    assert _stamp(home) is None


def test_hidden_widget_intent_and_in_process_submits_do_not_record(home):
    _submit({"session_id": "s", "text": "widget", "display_kind": "hidden"})
    _submit({"session_id": "s", "text": "start chat"}, rid=None)  # start_chat's in-process submit
    assert _stamp(home) is None


def test_classifier_rejects_relay_and_hosted_submits():
    assert human_input.is_human_typed_submit("r", {"text": "hi"}, "hi")
    for key in ("_turn_author", "_hosted_task", "_hosted_terminal_callback"):
        assert not human_input.is_human_typed_submit("r", {key: object()}, "hi"), key
    assert not human_input.is_human_typed_submit("r", {}, "   ")


# ── agent-marked websockets (/api/ws?client=agent, gateway_rpc.py) are not David typing (t_01a186c204de1d5b) ──


def _bound(client_kind=None):
    """Bind a stub WS transport, as handle_ws's dispatch does, for one prompt.submit."""
    from tui_gateway.transport import bind_transport

    return bind_transport(types.SimpleNamespace(client_kind=client_kind, auth_identity=None, _peer="t",
                                                write=lambda obj: True))


def test_agent_marked_ws_submit_does_not_record(home):
    from tui_gateway.transport import reset_transport

    token = _bound("agent")
    try:
        _submit({"session_id": "no-such-session", "text": "proof prompt from an agent"})
    finally:
        reset_transport(token)
    assert _stamp(home) is None, "a prompt.submit over /api/ws?client=agent must not stamp"


def test_normal_client_ws_submit_still_records(home):
    from tui_gateway.transport import reset_transport

    token = _bound(None)
    try:
        _submit({"session_id": "no-such-session", "text": "typed by David"})
    finally:
        reset_transport(token)
    stamp = _stamp(home)
    assert stamp is not None and stamp["session_id"] == "no-such-session"


def test_submit_into_agent_created_session_does_not_record(home, monkeypatch):
    # Belt and braces: a normal (unmarked) connection submitting into a chat an agent connection created.
    # (A running session, so the submit queues instead of starting a turn.)
    monkeypatch.setitem(server._sessions, "agent-sid", _session(types.SimpleNamespace(), agent_created=True))
    _submit({"session_id": "agent-sid", "text": "follow-up", "queued": True})
    assert _stamp(home) is None
    human_input.note_submit("r", {"session_id": "x", "text": "hi"}, "hi", session={"agent_created": False})
    assert _stamp(home) is not None, "control: a human-created session still stamps"


def test_hidden_param_path_unchanged_on_normal_transport(home):
    from tui_gateway.transport import reset_transport

    token = _bound(None)
    try:
        _submit({"session_id": "s", "text": "widget", "display_kind": "hidden"})
    finally:
        reset_transport(token)
    assert _stamp(home) is None


def test_ws_accept_records_client_kind(monkeypatch):
    import asyncio

    from tui_gateway import ws as ws_mod

    created = []
    real = ws_mod.WSTransport
    monkeypatch.setattr(ws_mod, "WSTransport", lambda ws, loop, **kw: created.append(real(ws, loop, **kw)) or created[-1])

    def fake(query, scope=None):
        class FakeWS:
            query_params = query

            async def accept(self):
                pass

            async def send_text(self, line):
                pass

            async def receive_text(self):
                raise ws_mod._WebSocketDisconnect()

            async def close(self, code=1000):
                pass

        ws = FakeWS()
        if scope is not None:
            ws.scope = scope  # real ASGI path: the raw query string
        return ws

    asyncio.run(ws_mod.handle_ws(fake({"token": "x", "client": "agent"})))
    asyncio.run(ws_mod.handle_ws(fake({"token": "x"})))
    asyncio.run(ws_mod.handle_ws(fake({}, scope={"type": "websocket", "query_string": b"token=x&client=agent"})))
    asyncio.run(ws_mod.handle_ws(fake({}, scope={"type": "websocket"})))
    from tui_gateway.transport import is_agent_transport

    assert [is_agent_transport(t) for t in created] == [True, False, True, False]


def test_agent_connection_session_create_defaults_hidden(home):
    from tui_gateway.transport import bind_transport, reset_transport

    for kind, params, want_hidden in (("agent", {}, True), ("agent", {"hidden": False}, False), (None, {}, False)):
        token = bind_transport(types.SimpleNamespace(client_kind=kind, auth_identity=None, write=lambda o: True))
        try:
            res = server._methods["session.create"]("r", dict(params, cols=80))
        finally:
            reset_transport(token)
        sid = (res.get("result") or {}).get("session_id")
        assert sid, res
        sess = server._sessions.pop(sid)
        assert sess["pending_hidden"] is want_hidden, (kind, params)
        assert sess["agent_created"] is (kind == "agent")


# ── injected turns run _run_prompt_submit directly: the turn pipeline itself never records ───────────────


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
    return {"agent": agent, "session_key": "session-key", "history": [], "history_lock": threading.Lock(),
            "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
            **extra}


@pytest.fixture()
def turn_env(monkeypatch, tmp_path, home):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    ran: list = []
    agent = types.SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None,
                                  run_conversation=lambda message, **kw: ran.append(message) or {"final_response": "ok"})
    return agent, ran


@pytest.mark.parametrize("display_kind", ["kanban_notice", "auto_continue"])
def test_injected_turn_does_not_record(turn_env, home, display_kind):
    agent, ran = turn_env
    server._run_prompt_submit("rid", "sid", _session(agent), "[injected] your card finished",
                              display_kind=display_kind)
    assert ran, "the injected turn ran"
    assert _stamp(home) is None


def test_kanban_wake_does_not_record(monkeypatch, home):
    ready = threading.Event()
    ready.set()
    live = {"history_lock": threading.RLock(), "running": False, "agent_ready": ready, "session_key": "k",
            "profile_home": None}
    submitted: list = []
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key, profile_home=None: ("sid", live))
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session: None)
    monkeypatch.setattr(server, "_notif_submit", lambda *a, **k: submitted.append(a) or True)
    assert server._wake_session_turn("k", "cards done", display_kind="kanban_notice") == "started"
    assert submitted and _stamp(home) is None


def test_auto_continue_schedule_does_not_record(monkeypatch, home):
    from tui_gateway.turn_marker import record_turn_start

    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_load_cfg", dict)
    submitted: list = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: submitted.append(kw))
    record_turn_start(home, "session-key", "fix the flaky test")
    session = _session(types.SimpleNamespace(), running=False)
    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is not None
    assert submitted and submitted[0]["display_kind"] == "auto_continue"
    assert _stamp(home) is None



# ── agent-declared transports (/api/ws?client=agent) are not a person typing [daveey] ───────────────────────


def _submit_on(client_kind, params, rid="r1"):
    from tui_gateway.transport import bind_transport, reset_transport

    transport = types.SimpleNamespace(client_kind=client_kind, auth_identity=None, _peer="t", write=lambda o: True)
    token = bind_transport(transport)
    try:
        return _submit(params, rid=rid)
    finally:
        reset_transport(token)


def test_agent_transport_submit_does_not_record(home):
    _submit_on("agent", {"session_id": "s", "text": "Run sleep 45 sequentially 150 times"})
    assert _stamp(home) is None


@pytest.mark.parametrize("client_kind", [None, "renderer"])
def test_renderer_transport_submit_still_records(home, client_kind):
    _submit_on(client_kind, {"session_id": "s", "text": "hello there"})
    stamp = _stamp(home)
    assert stamp is not None and stamp["session_id"] == "s"


def test_classifier_rejects_agent_transport():
    from tui_gateway.transport import bind_transport, reset_transport

    token = bind_transport(types.SimpleNamespace(client_kind="agent", write=lambda o: True))
    try:
        assert not human_input.is_human_typed_submit("r", {"text": "hi"}, "hi")
    finally:
        reset_transport(token)
    assert human_input.is_human_typed_submit("r", {"text": "hi"}, "hi")
