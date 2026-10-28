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
