"""[daveey] t_01a18ac84e3491a9: a Desktop turn blocked in ``clarify`` never reaches a step boundary, so the
drainer's pause request ran out its budget and the update cut the turn (hub 2026-10-30 05:37: a 41-min clarify,
then a crash-resume note). While a pause is requested for a turn the next backend auto-continues, the pending
clarify is withdrawn unanswered and the turn pauses at the boundary right after it.
"""

from __future__ import annotations

import json
import threading
import time
import types

import pytest

from hermes_cli import admission_fence as af
from tools import clarify_early
from tools.clarify_tool import clarify_tool
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}
QUESTIONS = [{"question": "Which runner pool?", "choices": ["org", "personal"]}]
WAIT_S = 10


class Blocking:
    """A surface callback that blocks until answered or cancelled, like the TUI bridge."""

    def __init__(self):
        self.event = threading.Event()
        self.entered = threading.Event()
        self.cancel_reasons: list = []

    def __call__(self, questions):
        self.entered.set()
        self.event.wait(WAIT_S)
        return {"answers": {}, "outcome": "cancelled"}

    def cancel_early(self, reason):
        self.cancel_reasons.append(reason)
        self.event.set()
        return True


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(clarify_early, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(up, "_clarify_provider_installed", False, raising=False)
    clarify_early.reset_for_tests()
    session = {"agent": types.SimpleNamespace(session_id="session-key"), "session_key": "session-key",
               "history_lock": threading.RLock(), "profile_home": str(tmp_path)}
    session["agent"].step_boundary_pause = up.boundary_pause_hook(session, "session-key")
    monkeypatch.setitem(server._sessions, "ui-sid", session)
    record_turn_start(tmp_path, "session-key", "the prompt")
    yield types.SimpleNamespace(home=tmp_path, session=session)
    clarify_early.reset_for_tests()


def _request_pause(root):
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 120)


def test_no_pause_request_keeps_the_clarify(env):
    assert up.clarify_pause_provider("session-key", "tc1") is None


def test_pause_request_withdraws_the_clarify_of_a_resumable_turn(env):
    _request_pause(env.home)
    early = up.clarify_pause_provider("session-key", "tc1")
    assert early is not None
    assert early.result["status"] == up.CLARIFY_PAUSE_STATUS


@pytest.mark.parametrize("case", ["no_hook", "opted_out", "other_session"])
def test_turn_that_will_not_pause_keeps_its_clarify(env, case):
    sid = "session-key"
    if case == "no_hook":
        env.session["agent"].step_boundary_pause = None
    if case == "opted_out":
        record_turn_start(env.home, "session-key", "the prompt", auto_continue=False)
    if case == "other_session":
        sid = "someone-else"
    _request_pause(env.home)
    assert up.clarify_pause_provider(sid, "tc1") is None


def test_blocked_clarify_ends_and_the_turn_pauses_at_the_next_boundary(env):
    """The hub incident end to end: the clarify is already blocked when the drainer asks for the pause."""
    # getattr keeps this test behavioural on a tree without the fix: there the clarify just keeps blocking.
    install = getattr(up, "install_clarify_pause_provider", lambda: None)
    install()
    install()  # idempotent
    surface = Blocking()
    box: dict = {}
    worker = threading.Thread(target=lambda: box.setdefault(
        "result", clarify_tool(QUESTIONS, callback=surface, session_id="session-key", tool_call_id="tc1")))
    worker.start()
    assert surface.entered.wait(WAIT_S)
    _request_pause(env.home)
    worker.join(WAIT_S)
    assert not worker.is_alive(), "clarify kept blocking while a pause was requested"

    result = json.loads(box["result"])
    assert result["status"] == up.CLARIFY_PAUSE_STATUS
    assert "ask it again" in result["notice"]
    assert surface.cancel_reasons == [up.CLARIFY_PAUSE_STATUS]
    # The step boundary right after the tool result now pauses the turn instead of the update cutting it.
    assert env.session["agent"].step_boundary_pause() == up.PAUSE_REASON
    marker = read_turn_marker(env.home, "session-key")
    assert marker is not None and marker["reason"] == up.PAUSE_REASON
