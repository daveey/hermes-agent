"""Durable provider-retry deadline through the REAL gateway scheduler.

``server._run_prompt_submit`` -> ``_invoke_agent`` installs the deadline hooks; ``_maybe_schedule_auto_continue``
resumes the paused turn on its own background thread. Nothing here installs a hook by hand. Fakes: the provider
(a 503 with a short real ``Retry-After``, then answers), agent construction (``_start_agent_build``/``_wait_agent``
hand the session an agent) and UI emit. Production pause policy is untouched; only the provider's Retry-After is short
(the genuine 600 s proof is a separate process test).
"""
import threading
import time
import types

import pytest

from hermes_cli import admission_fence as af
from tests.tui_gateway import test_update_pause_model_request as base
from tui_gateway import server, update_pause as up
from tui_gateway.turn_marker import read_turn_marker

RETRY_AFTER_S = 3.0
KEY = "session-key"
HOOKS = ("provider_retry_deadline_stamp", "provider_retry_deadline_clear", "provider_retry_not_before")


class _Boom(Exception):
    status_code = 503

    def __init__(self):
        super().__init__("service unavailable")
        self.response = types.SimpleNamespace(status_code=503, headers={"Retry-After": str(int(RETRY_AFTER_S))})


class _Provider:
    """request 1: a tool call; request 2: 503 + Retry-After; then plain answers. Records wall time of every call."""

    def __init__(self):
        self.times, self.failed = [], threading.Event()
        self.failed_at = None

    def __call__(self, *a, **kw):
        self.times.append(time.time())
        n = len(self.times)
        if n == 1:
            return base._response(base._call("call_1"))
        if n == 2 and self.failed_at is None:
            self.failed_at = time.time()
            self.failed.set()
            raise _Boom()
        return base._response()


def _session(agent, home):
    return {"agent": agent, "session_key": KEY, "history": [], "history_lock": threading.RLock(),
            "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
            "profile_home": str(home)}


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: base.CFG)
    for name, value in {"_emit": lambda *a, **k: None, "_wire_callbacks": lambda sid: None,
                        "_sync_agent_model_with_config": lambda sid, s: None,
                        "_session_cwd": lambda s: str(tmp_path), "_register_session_cwd": lambda s: None,
                        "_tts_stream_begin": lambda: None, "_sync_session_key_after_compress": lambda *a, **k: None,
                        "_get_usage": lambda a: {}}.items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(up, "start_in_place_watcher", lambda *a: None)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.3)
    monkeypatch.setattr(up, "MID_TOOL_POLL_S", 0.05)
    monkeypatch.setattr("model_tools.handle_function_call", lambda name, *a, **k: "quick result")
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    made = []

    def make(provider):
        agent = base._make_agent(tmp_path)
        agent.client.chat.completions.create.side_effect = provider
        made.append(agent)
        return agent

    yield tmp_path, make
    for agent in made:
        agent._session_db.close()


def _join_turn(session, timeout=30):
    thread = session.get("_run_thread")
    assert thread is not None
    thread.join(timeout)
    assert not thread.is_alive()


def _join_scheduler(timeout=40):
    for t in [t for t in threading.enumerate() if t.name.startswith("auto-continue-")]:
        t.join(timeout)
        assert not t.is_alive()


def _first_turn_paused(home, agent, provider):
    """Real scheduler runs the user's turn; the update pause lands while it waits out the provider's Retry-After."""
    session = _session(agent, home)
    stop = threading.Event()

    def issue():
        if provider.failed.wait(10):
            from tests.waiting import wait_until
            wait_until(lambda: (read_turn_marker(home, KEY) or {}).get("provider_retry_not_before"), timeout=5)
            af.set_hold(home)
            af.request_pause(home, expires_at=time.time() + 120)
        stop.wait(0)

    issuer = threading.Thread(target=issue)
    issuer.start()
    try:
        server._run_prompt_submit("rid", "ui-sid", session, "run the quick job")
        _join_turn(session)
    finally:
        issuer.join(15)
    assert not issuer.is_alive()
    marker = read_turn_marker(home, KEY)
    assert marker and marker["reason"] == up.PAUSE_REASON, marker
    deadline = marker["provider_retry_not_before"]
    assert provider.failed_at + RETRY_AFTER_S - 0.5 <= deadline <= provider.failed_at + RETRY_AFTER_S + 1.5
    assert len(provider.times) == 2, "the provider must not be called again during the pause"
    assert not any(h in vars(agent) for h in HOOKS), "hooks must be gone after the paused turn"
    assert up.pause_requested(session)
    af.clear_pause_request(home)
    af.clear_hold(home)
    return deadline


@pytest.mark.parametrize("mode", ["cold", "cached"])
def test_scheduler_restores_deadline_then_next_user_turn_is_undelayed(rig, monkeypatch, mode):
    home, make = rig
    provider = _Provider()
    agent = make(provider)
    deadline = _first_turn_paused(home, agent, provider)

    if mode == "cold":
        # A fresh agent (no hooks, no memory of the wait) and a fresh session object get built by the scheduler.
        resumed_agent = make(provider)
        fresh = {"session_key": KEY, "history": [], "history_lock": threading.RLock(), "running": False,
                 "history_version": 0, "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None,
                 "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
                 "profile_home": str(home)}
    else:
        resumed_agent = agent  # in-place: the same cached agent object serves the continuation
        fresh = _session(agent, home)
        fresh["running"] = False

    def install(sid, session):
        session["agent"] = resumed_agent

    monkeypatch.setattr(server, "_start_agent_build", install)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)
    calls_before = len(provider.times)

    assert server._maybe_schedule_auto_continue("fresh-sid", fresh, KEY) is not None
    _join_scheduler()
    _join_turn(fresh)

    resumed_calls = provider.times[calls_before:]
    assert resumed_calls, "continuation never reached the provider"
    assert resumed_calls[0] >= deadline - 0.05, (resumed_calls[0], deadline)  # no early provider call
    assert resumed_calls[0] - deadline < 3.0
    assert read_turn_marker(home, KEY) is None, "marker (and its deadline) must be gone after the served wait"
    assert not any(h in vars(resumed_agent) for h in HOOKS)
    assert "_auto_continue_provider_retry_not_before" not in fresh

    # The next, ordinary user turn inherits neither deadline nor hooks: its provider call is immediate.
    fresh["running"] = True
    started = time.time()
    server._run_prompt_submit("rid2", "fresh-sid", fresh, "thanks, anything else?")
    _join_turn(fresh)
    assert len(provider.times) > calls_before + len(resumed_calls)
    assert provider.times[calls_before + len(resumed_calls)] - started < 1.5
    assert read_turn_marker(home, KEY) is None
    assert not any(h in vars(resumed_agent) for h in HOOKS)
