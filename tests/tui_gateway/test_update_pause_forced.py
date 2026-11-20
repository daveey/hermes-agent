"""[daveey] "Force update": while hermes-sync serves a force request its pause request carries ``"force": true``.

A turn the next backend would not auto-continue (``not_resumable_reason``: a user-typed turn with
``auto_continue=false``, resume on startup off, ...) used to keep its running tool call and its admission
reservation through the whole drain, so the update waited on it (hermes-hub 2026-11-20: 25+ min). Under a forced
pause its in-flight calls are cancelled after the same ``MID_TOOL_GRACE_S`` and the turn ends at the next step
boundary with ``FORCED_STOP_REASON``: nothing keeps its marker, so it ends like a stopped turn and is not continued
(the user sends it again). Resumable turns pause and auto-continue exactly as without force.

Same harness as ``test_update_pause_mid_tool``: a real ``AIAgent`` turn, real tool executor, real SessionDB and the
gateway's real hook and watcher; only the model and the tool bodies are fakes.
"""

from __future__ import annotations

import logging
import time

from hermes_cli import admission_fence as af
from tests.tui_gateway import test_update_pause_mid_tool as mid
from tests.tui_gateway.test_update_pause_mid_tool import (
    CFG, STAMP, TOOL_CAP_S, _Agent, _call, _durable_tools, _long_call, _make_agent, _response, _run_turn, _session,
)
from agent import update_pause_cancel
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

home = mid.home
_quiet = mid._quiet
NOT_RESUMABLE = {"desktop": {"auto_continue": dict(CFG["desktop"]["auto_continue"], resume_on_startup=False)}}


def _request_pause(root, *, force):
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 120, force=force)


def _not_resumable(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: NOT_RESUMABLE)


def test_forced_pause_cancels_a_non_resumable_turns_call_and_ends_the_turn(home, tmp_path, monkeypatch, caplog):
    _not_resumable(monkeypatch)
    record_turn_start(home, "session-key", "run the long job")
    agent = _make_agent(tmp_path, "long_job")
    session = _session(agent, home)
    _request_pause(home, force=True)
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: _long_call())

    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="tui_gateway.update_pause"):
        result = _run_turn(agent, session, [_response(_call("long_job", "call_1")), _response(), _response()])

    assert time.monotonic() - started < TOOL_CAP_S / 2  # cancelled after the grace, not run to its own end
    assert agent.client.chat.completions.create.call_count == 1  # the turn ends: no model call after the cancel
    assert result["interrupted"] is True and result["paused_for_update"] == up.FORCED_STOP_REASON
    assert STAMP.match(_durable_tools(home)["call_1"])  # the cancelled call's result is persisted
    assert not agent._interrupt_requested
    messages = [r.getMessage() for r in caplog.records]
    assert any("core-update pause (forced): session session-key cancels the running tool call(s)" in m
               for m in messages)
    assert "core-update pause (forced): session session-key stops its non-resumable turn " \
           "(desktop.auto_continue.resume_on_startup is off)" in messages

    # Not a pause: the session keeps no marker for a continuation, and the turn end retires it like a Stop's.
    assert not up.pause_stands(session, "session-key", result)
    assert "_update_pause_marker_key" not in session
    marker = read_turn_marker(home, "session-key")
    assert marker.get("reason") != up.PAUSE_REASON
    server._retire_turn_marker(session, "session-key")
    assert read_turn_marker(home, "session-key") is None


def test_forced_pause_ends_a_non_resumable_turn_at_the_next_boundary(home, tmp_path, monkeypatch):
    _not_resumable(monkeypatch)
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "quick")
    _request_pause(home, force=True)
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: "quick result")
    result = _run_turn(agent, _session(agent, home), [_response(_call("quick", "call_1")), _response()])
    assert result["paused_for_update"] == up.FORCED_STOP_REASON and result["interrupted"] is True
    assert _durable_tools(home)["call_1"] == "quick result"  # finished calls keep their real results
    assert agent.client.chat.completions.create.call_count == 1


def test_unforced_pause_still_leaves_a_non_resumable_turn_running(home, tmp_path, monkeypatch):
    _not_resumable(monkeypatch)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.05)
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "long_job")
    _request_pause(home, force=False)

    def _one_second(*_a, **_k):
        time.sleep(1.0)  # health: allow HX013 -- the call must outlast the grace, so a cancel would land in it
        return "ran to the end"

    monkeypatch.setattr("model_tools.handle_function_call", _one_second)
    result = _run_turn(agent, _session(agent, home), [_response(_call("long_job", "call_1")), _response()])
    assert _durable_tools(home)["call_1"] == "ran to the end"
    assert not result.get("paused_for_update") and result["completed"] is True
    assert agent.client.chat.completions.create.call_count == 2


def test_forced_pause_pauses_a_resumable_turn_exactly_as_before(home, tmp_path, monkeypatch):
    record_turn_start(home, "session-key", "run the long job")
    agent = _make_agent(tmp_path, "long_job")
    session = _session(agent, home)
    _request_pause(home, force=True)
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: _long_call())
    result = _run_turn(agent, session, [_response(_call("long_job", "call_1")), _response()])
    assert result["paused_for_update"] == up.PAUSE_REASON
    assert STAMP.match(_durable_tools(home)["call_1"])
    marker = read_turn_marker(home, "session-key")
    assert marker["reason"] == up.PAUSE_REASON and marker["pauses"] == 1
    assert session["_update_pause_marker_key"] == "session-key"  # kept for the continuation
    server._retire_turn_marker(session, "session-key")
    assert read_turn_marker(home, "session-key") is not None


# ── The watcher's poll and the boundary hook, unit by unit ────────────────────────────────────────────


def _step(home, agent, state, monkeypatch, now):
    monkeypatch.setattr(up, "_mono", lambda: now)
    return up.mid_tool_cancel_step(_session(agent, home), "session-key", agent, state)


def test_poll_cancels_a_non_resumable_call_only_when_forced(home, monkeypatch):
    _not_resumable(monkeypatch)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 30.0)
    record_turn_start(home, "session-key", "p")
    _request_pause(home, force=False)
    agent, state = _Agent(), {}
    vars(agent)["_update_pause_in_flight"] = {"c1": 0.0}
    assert _step(home, agent, state, monkeypatch, 10.0) is None  # first sighting
    assert _step(home, agent, state, monkeypatch, 100.0) is None and agent.interrupts == []  # declined
    assert state["declined"] == "desktop.auto_continue.resume_on_startup is off"

    _request_pause(home, force=True)  # the drainer starts serving a force request
    assert _step(home, agent, state, monkeypatch, 100.0) == ["c1"]
    assert agent.interrupts == [update_pause_cancel.CANCEL_TOOL_REASON]


def test_forced_poll_keeps_the_grace(home, monkeypatch):
    _not_resumable(monkeypatch)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 30.0)
    record_turn_start(home, "session-key", "p")
    _request_pause(home, force=True)
    agent, state = _Agent(), {}
    vars(agent)["_update_pause_in_flight"] = {"c1": 50.0}
    assert _step(home, agent, state, monkeypatch, 60.0) is None
    assert _step(home, agent, state, monkeypatch, 89.9) is None and agent.interrupts == []  # 30 s after sighting
    assert _step(home, agent, state, monkeypatch, 90.0) == ["c1"]


def test_hook_stops_a_non_resumable_turn_only_when_forced(home, monkeypatch):
    _not_resumable(monkeypatch)
    record_turn_start(home, "session-key", "p")
    session = _session(_Agent(), home)
    hook = up.boundary_pause_hook(session, "session-key")
    _request_pause(home, force=False)
    assert hook() is None
    _request_pause(home, force=True)
    assert hook() == up.FORCED_STOP_REASON
    assert "_update_pause_marker_key" not in session  # nothing kept for a continuation
    assert read_turn_marker(home, "session-key").get("reason") != up.PAUSE_REASON
    af.clear_pause_request(home)
    assert hook() is None  # no request, no stop
