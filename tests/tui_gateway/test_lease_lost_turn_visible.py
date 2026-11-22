"""[daveey] A turn the system stopped because it lost its session lease must be visible.

Before this, a ``session_turn_lease_lost`` interrupt reached Desktop as ``status=interrupted`` with
empty text. That is the shape of a user pressing Stop, so the transcript showed nothing and the
session row went idle. The user only saw the agent stop mid-work. The gateway now turns that
interrupt into a retryable failure: the error card in the transcript, a retained inflight snapshot,
and ``status=error error_retained=True`` in the turn bookend. A human stop keeps today's silent
cancel.
"""

from __future__ import annotations

import logging
import threading
import types

import pytest
pytestmark = pytest.mark.usefixtures("no_background_link_refresh")

from tui_gateway import server


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


def _session(agent):
    return {
        "agent": agent, "session_key": "gw-session-key", "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
        "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
    }


@pytest.fixture()
def emitted(monkeypatch, tmp_path):
    frames = []
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None, *a, **k: frames.append((event, payload)))
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    return frames


def _run(result):
    agent = types.SimpleNamespace(
        session_id="agent-sid-1", run_conversation=lambda *a, **k: result, clear_interrupt=lambda: None)
    session = _session(agent)
    server._run_prompt_submit("rid", "ui-sid", session, "go")
    return session


def _complete(frames):
    payloads = [p for event, p in frames if event == "message.complete"]
    assert len(payloads) == 1
    return payloads[0]


def _finished(caplog):
    return [r.getMessage() for r in caplog.records if "tui turn finished" in r.getMessage()][-1]


LEASE_MESSAGE = "Session turn lease could not be refreshed; stopping to protect the transcript."


def test_lease_lost_interrupt_renders_a_visible_retryable_failure(emitted, caplog):
    with caplog.at_level(logging.INFO, logger="tui_gateway.server"):
        session = _run({
            "final_response": "", "messages": [], "interrupted": True, "completed": False,
            "interrupt_issuer": "session_turn_lease_lost", "interrupt_message": LEASE_MESSAGE,
        })
    payload = _complete(emitted)
    assert payload["status"] == "error"
    assert payload["recoverable"] is True
    assert "lost ownership of the session" in payload["text"]
    assert LEASE_MESSAGE in payload["text"]
    assert payload["error"] == LEASE_MESSAGE
    assert payload["error_surface"]["code"] == "session_turn_lease_lost"
    assert payload["error_surface"]["retryable"] is True
    assert payload["error_surface"]["layer"] == "gateway"
    # Retained for resume: a reload/reconnect still shows the failure.
    assert session["inflight_turn"] is not None
    msg = _finished(caplog)
    assert "status=error" in msg and "error_retained=True" in msg


def test_lease_lost_keeps_text_already_produced_as_a_partial(emitted):
    _run({
        "final_response": "Half of the answer", "messages": [], "interrupted": True,
        "interrupt_issuer": "session_turn_lease_lost", "interrupt_message": LEASE_MESSAGE,
    })
    payload = _complete(emitted)
    assert payload["status"] == "error"
    assert payload["partial"] is True
    assert payload["text"] == "Half of the answer"


def test_a_human_stop_stays_a_silent_cancel(emitted, caplog):
    """Control: no system issuer, so nothing changes for Stop / a new message."""
    with caplog.at_level(logging.INFO, logger="tui_gateway.server"):
        _run({"final_response": "", "messages": [], "interrupted": True, "interrupt_message": "next msg"})
    payload = _complete(emitted)
    assert payload["status"] == "interrupted"
    assert payload["text"] == ""
    assert "error" not in payload
    assert "status=interrupted" in _finished(caplog)
