"""[daveey] "Force update": a non-resumable turn blocked on a pending approval or clarify never reaches a step
boundary, so a forced pause withdraws those prompts too (with a notice that the turn does not continue), exactly as
an unforced pause does for a resumable turn. The incident's turn was user-typed with ``auto_continue=false``."""

from __future__ import annotations

import time

from agent import update_pause_cancel
from hermes_cli import admission_fence as af
from tests.tui_gateway import test_update_pause_clarify as helpers
from tests.tui_gateway.test_update_pause_approval import _cleanup, _pending
from tools import approval
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

env = helpers.env


def _opted_out(env):
    record_turn_start(env.home, "session-key", "the prompt", auto_continue=False)


def _forced_pause(root):
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 120, force=True)


def test_forced_pause_withdraws_an_opted_out_turns_approval(env, monkeypatch):
    _opted_out(env)
    thread, box, settled = _pending(monkeypatch, "session-key")
    try:
        assert update_pause_cancel.in_flight(env.session["agent"]) == {}
        _forced_pause(env.home)
        up.mid_tool_cancel_step(env.session, "session-key", env.session["agent"], {})
        thread.join(2)
        assert not thread.is_alive(), "a pending approval still blocks the forced update"
        assert box["result"]["choice"] is None
        assert "does not continue automatically" in box["result"]["cancelled"]
        assert approval.list_gateway_approvals("session-key") == []
        assert settled == ["session_closed"]
    finally:
        _cleanup(thread, box, "session-key")


def test_unforced_pause_keeps_an_opted_out_turns_approval(env, monkeypatch):
    _opted_out(env)
    thread, box, settled = _pending(monkeypatch, "session-key")
    try:
        helpers._request_pause(env.home)
        up.mid_tool_cancel_step(env.session, "session-key", env.session["agent"], {})
        assert approval.has_blocking_approval("session-key") and settled == []
    finally:
        _cleanup(thread, box, "session-key")


def test_forced_pause_withdraws_an_opted_out_turns_clarify_and_stops_the_turn(env):
    _opted_out(env)
    helpers._request_pause(env.home)
    assert up.clarify_pause_provider("session-key", "tc1") is None  # unforced: the clarify stays

    _forced_pause(env.home)
    early = up.clarify_pause_provider("session-key", "tc1")
    assert early is not None and early.result["status"] == up.CLARIFY_PAUSE_STATUS
    assert early.result["notice"] == up.CLARIFY_FORCED_NOTICE
    # The boundary right after it ends the turn, without a pause a continuation would pick up.
    assert env.session["agent"].step_boundary_pause() == up.FORCED_STOP_REASON
    assert read_turn_marker(env.home, "session-key").get("reason") != up.PAUSE_REASON


def test_forced_pause_withdraws_a_resumable_turns_clarify_with_the_usual_notice(env):
    _forced_pause(env.home)
    early = up.clarify_pause_provider("session-key", "tc1")
    assert early is not None and early.result["notice"] == up.CLARIFY_PAUSE_NOTICE
    assert env.session["agent"].step_boundary_pause() == up.PAUSE_REASON
