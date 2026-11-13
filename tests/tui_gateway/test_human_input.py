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


@pytest.mark.parametrize("session", [{"agent_created": True}, {"room_plumbing": True}])
def test_input_origin_agent_for_agent_created_or_room_plumbing_sessions(session):
    assert human_input.input_origin("r", {}, "hi", session=session) == "agent"


def _stored(tmp_path, key, *, title, hidden, model_config=None):
    """A session dict whose row lives in its own profile ``state.db`` (no ``room_plumbing``/``pending_hidden`` in
    memory: a resumed session, or one whose transient ``pending_hidden`` was already applied and popped)."""
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session(key, source="desktop", model_config=model_config)
        db.set_session_title(key, title)
        db.set_session_hidden(key, hidden)
    finally:
        db.close()
    return {"session_key": key, "profile_home": str(tmp_path)}


@pytest.mark.parametrize("title,model_config", [("Group: standup", None), ("renamed", {"room_plumbing": True})])
def test_input_origin_agent_for_every_submit_into_a_stored_room_plumbing_row(tmp_path, title, model_config):
    session = _stored(tmp_path, "plumb", title=title, hidden=True, model_config=model_config)
    assert [human_input.input_origin("r", {}, "hi", session=session) for _ in range(2)] == ["agent", "agent"]


@pytest.mark.parametrize("row,expected", [
    (None, False), ({}, False),
    ({"model_config": {"room_plumbing": True}}, True), ({"model_config": '{"room_plumbing": true}'}, True),
    ({"model_config": "not json", "hidden": 1, "title": "Bot Chat"}, False), ({"model_config": "[1]"}, False),
    ({"model_config": "  "}, False), ({"hidden": 1, "title": "  Group: a"}, True), ({"hidden": 0, "title": "Group: a"}, False),
])
def test_row_is_room_plumbing(row, expected):
    assert human_input._row_is_room_plumbing(row) is expected


_PARITY_CONFIGS = [None, {}, {"room_plumbing": True}, {"room_plumbing": False}, {"room_plumbing": 1}, {"room_plumbing": ""},
                   '{"room_plumbing": true}', '{"room_plumbing": 0}', "not json", "[1]", "null", "  "]
_PARITY_TITLES = [None, "", "Group: standup", "  Group: a", "group: lower", "Groups", "renamed"]
_PARITY_HIDDEN = [None, 0, 1, True, False]


def test_row_is_room_plumbing_matches_the_resume_rule():
    """One plumbing rule: ``_row_is_room_plumbing`` mirrors ``agent_factory._stored_session_runtime_overrides`` (which
    drops a plumbing row's stored runtime on resume; upstream code, left untouched), so the two must not drift. A row
    carrying a stored model restores a non-empty override unless the resume rule calls it plumbing (agent_factory
    binds into the server namespace, so call it there)."""
    rows = [{"id": "s", "model": "parity-model", "model_config": config, "title": title, "hidden": hidden}
            for config in _PARITY_CONFIGS for title in _PARITY_TITLES for hidden in _PARITY_HIDDEN]
    resume_plumbing = [server._stored_session_runtime_overrides(row) == {} for row in rows]
    assert any(resume_plumbing) and not all(resume_plumbing), "the matrix must hold both kinds of row"
    mismatched = [(row, resume) for row, resume in zip(rows, resume_plumbing)
                  if human_input._row_is_room_plumbing(row) is not resume]
    assert mismatched == []


def test_input_origin_reads_only_the_plumbing_columns(tmp_path, monkeypatch, caplog):
    """A would-be human submit reads ``model_config, hidden, title`` for the key: never ``get_session`` (its
    flush_for_read plus prompt/tool JOINs on the request thread) or any other full-row read."""
    import logging

    from hermes_state import SessionDB

    plumbing = _stored(tmp_path, "plumb", title="Group: x", hidden=True)
    chat = _stored(tmp_path, "dm", title="Bot Chat", hidden=True, model_config={"room_plumbing": False})
    reads, sql = [], []

    def spy(name):
        def call(self, *args, **kwargs):
            reads.append(name)
            raise AssertionError(f"{name} on a submit")
        return call

    real_read_one = SessionDB._read_one

    def read_one(self, query, params=()):
        sql.append(" ".join(query.split()))
        return real_read_one(self, query, params)

    for name in ("get_session", "flush_for_read", "get_session_list_row"):
        monkeypatch.setattr(SessionDB, name, spy(name))
    monkeypatch.setattr(SessionDB, "_read_one", read_one)
    caplog.set_level(logging.DEBUG, logger=human_input.logger.name)
    assert human_input.input_origin("r", {}, "hi", session=plumbing) == "agent"
    assert human_input.input_origin("r", {}, "hi", session=chat) == "human"
    assert human_input.input_origin("r", {}, "hi", session={"session_key": "missing", "profile_home": str(tmp_path)}) \
        == "human", "no row: not plumbing"
    assert reads == []
    assert "row read failed" not in caplog.text, "a missing row is a plain miss, not an error"
    assert sql == ["SELECT model_config, hidden, title FROM sessions WHERE id = ?"] * 3


@pytest.mark.parametrize("session", [{"pending_hidden": True}, {"pending_hidden": False}])
def test_hidden_alone_is_no_authorship_signal(session):
    """Bot Mode's canonical Bot Chat is born hidden and David types into it, so hidden != agent (first submit
    included): only plumbing markers are."""
    assert human_input.input_origin("r", {}, "hi", session=session) == "human"


def test_input_origin_human_in_a_stored_hidden_bot_chat(tmp_path):
    session = _stored(tmp_path, "dm", title="Bot Chat", hidden=True)
    assert [human_input.input_origin("r", {}, "hi", session=session) for _ in range(2)] == ["human", "human"]
    assert human_input.input_origin("r", {}, "hi", session=_stored(tmp_path, "g", title="Group: x", hidden=False)) \
        == "human", "a visible row titled like a room is a user's chat"


def test_input_origin_row_lookup_failure_falls_back_to_human(monkeypatch):
    def boom(session):
        raise RuntimeError("db gone")

    monkeypatch.setattr(server, "_session_db", boom)
    assert human_input.input_origin("r", {}, "hi", session={"session_key": "k"}) == "human"


def test_input_origin_row_lookup_needs_a_key_and_an_open_db(monkeypatch, caplog):
    import contextlib
    import logging

    opened = []

    @contextlib.contextmanager
    def no_db(session):
        opened.append(session.get("session_key"))
        yield None

    monkeypatch.setattr(server, "_session_db", no_db)
    with caplog.at_level(logging.DEBUG, logger=human_input.logger.name):
        assert human_input.input_origin("r", {}, "hi", session={"cols": 80}) == "human"
        assert human_input.input_origin("r", {}, "hi", session={"session_key": "k"}) == "human"
    assert opened == ["k"], "a keyless (unpersisted) session has no row to read"
    assert "row read failed" not in caplog.text, "an unavailable store is a plain miss, not an error"


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
    assert human_input.note_submit("r", {"session_id": "b"}, "hi", session={"room_plumbing": True}) == "agent"
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


# ── compute-host isolated dispatch forwards (and consumes) the origin ─────────────────────────────────────


def _isolated_dispatch(monkeypatch, session, response):
    calls = []
    monkeypatch.setattr(server, "_reopen_before_isolated_submit", lambda sid, session: None)
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host",
                        lambda rid, sid, session, text, **kw: calls.append(kw) or response)
    session["_submit_input_origin"] = "agent"
    return calls


def test_isolated_dispatch_forwards_and_consumes_the_origin(monkeypatch):
    session = _session(None)
    calls = _isolated_dispatch(monkeypatch, session, server._ok("r1", {"status": "streaming"}))
    resp = server._dispatch_admitted_prompt("r1", "sid", session, "hi", True, None, None, None, {}, None)
    assert resp["result"]["status"] == "streaming"
    assert calls[0].get("input_origin") == "agent"
    assert "_submit_input_origin" not in session, "the remote turn consumed it: nothing lingers for the next turn"


def test_isolated_dispatch_failure_keeps_the_origin_for_the_inline_fallback(monkeypatch):
    session = _session(None)
    _isolated_dispatch(monkeypatch, session, server._err("r1", 5019, "compute-host dispatch failed: x"))
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a, **k: True)
    seen = []
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: seen.append(session.get("_submit_input_origin")))
    server._dispatch_admitted_prompt("r1", "sid", session, "hi", True, None, None, None, {}, None)
    assert seen == ["agent"], "the inline turn (_run_after_agent_ready) still reads this submit's origin"


def test_compute_host_turn_frame_carries_the_origin():
    session = _session(None)
    assert server._compute_host_turn_frame("r", "sid", session, "hi", input_origin="agent")["input_origin"] == "agent"
    assert "input_origin" not in server._compute_host_turn_frame("r", "sid", session, "hi")


def test_compute_host_dispatch_sends_the_origin_in_its_frame(monkeypatch):
    frames = []

    class _Supervisor:
        def submit_turn(self, frame, *, on_complete=None):
            frames.append(frame)

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _Supervisor())
    resp = server._submit_prompt_to_compute_host("r1", "sid", _session(None), "hi", input_origin="agent")
    assert resp["result"]["status"] == "streaming" and frames[0]["input_origin"] == "agent"


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
