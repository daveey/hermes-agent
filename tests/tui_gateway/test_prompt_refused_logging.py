"""[daveey] Every "tui prompt accepted" ends in "tui turn finished" or "tui prompt refused".

A continuation refused at admission (the update hold re-taken during its start-up) left only its "accepted" line
in agent.log: nothing said it never ran, so a 40-minute stall read as a running turn (defect gateway-833d39a686).
"""

from __future__ import annotations

import logging
import threading
import types

from tui_gateway import server
import pytest

pytestmark = pytest.mark.usefixtures("no_background_link_refresh")


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


def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


def _session(ran):
    agent = types.SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None,
                                  run_conversation=lambda message, **kw: ran.append(message) or
                                  {"final_response": "done"})
    return {"agent": agent, "session_key": "session-key", "history": [], "history_lock": threading.Lock(),
            "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None}


def _bookends(caplog):
    msgs = [r.getMessage() for r in caplog.records]
    return ([m for m in msgs if m.startswith("tui prompt accepted")],
            [m for m in msgs if m.startswith(("tui turn finished", "tui prompt refused"))])


def test_accepted_prompt_refused_at_admission_logs_its_refusal(monkeypatch, tmp_path, caplog):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_prepare_session_work", lambda *a, **k: None)  # update admission held
    ran: list = []
    caplog.set_level(logging.INFO)

    assert server._run_prompt_submit("rid", "sid", _session(ran), "go", display_kind="auto_continue") is False

    assert not ran
    accepted, closed = _bookends(caplog)
    assert len(accepted) == 1
    assert closed, "an accepted prompt that never started must say so"
    assert closed[0].startswith("tui prompt refused") and "cause=admission" in closed[0]
    assert "kind=auto_continue" in closed[0]


class _Work:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_prompt_refused_while_closing_names_closing(monkeypatch, tmp_path, caplog):
    _env(monkeypatch, tmp_path)
    work = _Work()
    ran: list = []
    session = _session(ran)

    def prepare(*a, **k):
        session["_closing"] = True  # the chat closed after the prompt was accepted, before its start
        return work

    monkeypatch.setattr(server, "_prepare_session_work", prepare)
    caplog.set_level(logging.INFO)

    assert server._run_prompt_submit("rid", "sid", session, "go") is False

    assert not ran and work.cancelled
    _, closed = _bookends(caplog)
    assert len(closed) == 1 and "cause=closing" in closed[0] and "kind=user" in closed[0]


def test_prompt_refused_for_a_replaced_session_names_not_started(monkeypatch, tmp_path, caplog):
    _env(monkeypatch, tmp_path)
    work = _Work()
    monkeypatch.setattr(server, "_prepare_session_work", lambda *a, **k: work)
    monkeypatch.setitem(server._sessions, "sid", {"other": True})  # the sid now routes to another session
    ran: list = []
    caplog.set_level(logging.INFO)

    assert server._run_prompt_submit("rid", "sid", _session(ran), "go") is False

    assert not ran and work.cancelled
    _, closed = _bookends(caplog)
    assert len(closed) == 1 and "cause=not_started" in closed[0]


def test_accepted_prompt_that_runs_logs_exactly_one_finished(monkeypatch, tmp_path, caplog):
    _env(monkeypatch, tmp_path)
    ran: list = []
    caplog.set_level(logging.INFO)

    assert server._run_prompt_submit("rid", "sid", _session(ran), "go") is True

    assert ran == ["go"] or len(ran) == 1
    accepted, closed = _bookends(caplog)
    assert len(accepted) == 1 and len(closed) == 1 and closed[0].startswith("tui turn finished")
