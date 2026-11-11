"""[daveey] t_01a1c7cf6f171e47: a core-update pause no longer waits out a long tool call.

``MID_TOOL_GRACE_S`` after the later of the pause request's first sighting and the call's start, a still-running
call of an auto-continuable turn is cancelled through the user-Stop interrupt path. Its result is persisted as
``[interrupted by Hermes update at <UTC>: re-run if still needed]`` and the turn pauses at the step boundary
right after it, exactly as a between-step pause: marker kept with ``reason: core_update_pause``, then the next
backend's startup sweep continues it with ``pause_note``. Finished calls of a parallel step keep their real
results; turns ``not_resumable_reason`` rejects are never cancelled.

These drive a real ``AIAgent`` turn through the real tool executor (sequential and concurrent), a real
SessionDB, and the gateway's real hook and watcher; only the model and the tool bodies are fakes.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent import update_pause_cancel
from hermes_cli import admission_fence as af
from hermes_state import SessionDB
from run_agent import AIAgent
from tools.interrupt import is_interrupted
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}
STAMP = re.compile(r"^\[interrupted by Hermes update at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ: re-run if still needed\]$")
TOOL_CAP_S = 8.0  # a fake call left alone runs this long, then returns its own result


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.3)
    monkeypatch.setattr(up, "MID_TOOL_POLL_S", 0.05)
    return tmp_path


def _defs(*names):
    return [{"type": "function", "function": {"name": n, "description": n, "parameters": {
        "type": "object", "properties": {}}}} for n in names]


def _make_agent(tmp_path: Path, *names: str) -> AIAgent:
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-test-home-", dir=tmp_path))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch("model_tools.get_tool_definitions", return_value=_defs(*names)),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="session-key", source="tui", model="test/model")
    agent._session_db, agent._session_db_created, agent.session_id = db, True, "session-key"
    agent._last_flushed_db_idx, agent._flushed_db_message_ids = 0, set()
    agent._flushed_db_message_session_id, agent._persist_disabled = None, False
    return agent


def _call(name, call_id):
    return types.SimpleNamespace(id=call_id, type="function",
                                 function=types.SimpleNamespace(name=name, arguments="{}"))


def _response(*calls):
    message = types.SimpleNamespace(content=None if calls else "done", reasoning_content=None, reasoning=None,
                                    tool_calls=list(calls) or None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=message, finish_reason="tool_calls" if calls else "stop")], model="test/model", usage=None)


def _long_call(started: threading.Event | None = None) -> str:
    """Like a foreground terminal command: runs until interrupted (or its own cap)."""
    if started is not None:
        started.set()
    deadline = time.monotonic() + TOOL_CAP_S
    while time.monotonic() < deadline:
        if is_interrupted():
            return json.dumps({"output": "partial", "exit_code": 130, "error": "interrupted"})
        time.sleep(0.02)
    return "finished on its own"


def _session(agent, home):
    return {"agent": agent, "session_key": "session-key", "profile_home": str(home),
            "history_lock": threading.RLock()}


def _run_turn(agent, session, responses):
    """The gateway's per-turn wiring (prompt_turn._invoke_agent): hook + watcher around run_conversation."""
    agent.client.chat.completions.create.side_effect = responses
    hook = up.boundary_pause_hook(session, "session-key")
    calls = session.setdefault("_test_hook_calls", [])
    agent.step_boundary_pause = lambda: calls.append(1) or hook()
    stop = up.start_mid_tool_cancel(session, "session-key", agent)
    try:
        return agent.run_conversation("run the long job")
    finally:
        stop()
        agent.step_boundary_pause = None


def _durable_tools(home):
    db = SessionDB(db_path=home / "state.db")
    try:
        return {m["tool_call_id"]: m["content"] for m in db.get_messages_as_conversation("session-key")
                if m["role"] == "tool"}
    finally:
        db.close()


def _request_pause(root):
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 120)


def test_capability_flag_is_text_greppable():
    text = Path(up.__file__).read_text(encoding="utf-8")
    assert re.search(r"^MID_TOOL_CANCEL = True$", text, re.MULTILINE)
    assert re.search(r"^MID_TOOL_GRACE_S = 30\.0$", text, re.MULTILINE)


def test_stamp_format():
    assert up.interrupted_tool_result(0) == "[interrupted by Hermes update at 1970-01-01T00:00:00Z: re-run if still needed]"


def test_long_tool_call_is_cancelled_after_the_grace_and_the_turn_pauses_resumably(home, tmp_path, monkeypatch):
    record_turn_start(home, "session-key", "run the long job")
    agent = _make_agent(tmp_path, "long_job")
    session = _session(agent, home)
    _request_pause(home)
    monkeypatch.setattr("model_tools.handle_function_call", lambda name, *a, **k: _long_call())
    monkeypatch.setattr(agent._subdirectory_hints, "check_tool_call", lambda *a, **k: "\n[hint: AGENTS.md here]")

    started = time.monotonic()
    result = _run_turn(agent, session, [_response(_call("long_job", "call_1")), _response(), _response()])
    took = time.monotonic() - started

    assert took < TOOL_CAP_S / 2  # cancelled after the grace, not run to its own end
    assert agent.client.chat.completions.create.call_count == 1  # no model call after the cancel
    assert result["interrupted"] is True and result["paused_for_update"] == up.PAUSE_REASON
    assert STAMP.match(_durable_tools(home)["call_1"])  # persisted exactly, in the canonical store
    assert session["_test_hook_calls"] == [1]  # one boundary pause: the marker is written and jobs stopped once
    marker = read_turn_marker(home, "session-key")
    assert marker["reason"] == "core_update_pause" and marker["pauses"] == 1 and marker["attempts"] == 0
    assert session["_update_pause_marker_key"] == "session-key"  # the turn end keeps the marker
    server._retire_turn_marker(session, "session-key")
    assert read_turn_marker(home, "session-key") is not None
    assert not agent._interrupt_requested  # the cancel's interrupt does not leak into the next turn

    # A fresh backend after the update: the startup sweep picks it up and continues with the pause note.
    from tui_gateway import startup_resume as sr
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries["session-key"]["writer_pid"] = 2 ** 22 - 3  # the pre-update backend has exited
    entries["session-key"].pop("writer_start_time", None)
    path.write_text(json.dumps(entries))
    assert [r["session_key"] for r in sr.pending_resumes(home, CFG)] == ["session-key"]
    submitted = []
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, s: None)
    monkeypatch.setattr(server, "_wait_agent", lambda s, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, s, text, **kw: submitted.append(text))
    fresh = {"agent": types.SimpleNamespace(), "session_key": "session-key", "history": [],
             "history_lock": threading.RLock(), "running": False}
    assert server._maybe_schedule_auto_continue("sid", fresh, "session-key") is not None
    assert submitted == [up.pause_note("run the long job")]


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


def test_parallel_step_cancels_only_the_running_calls(home, tmp_path, monkeypatch):
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "session_search", "skills_list")
    _request_pause(home)

    def _invoke(name, args, task_id, call_id, **_kw):
        return _long_call() if name == "session_search" else "the real listing"

    monkeypatch.setattr(agent, "_invoke_tool", _invoke)
    result = _run_turn(agent, _session(agent, home), [
        _response(_call("session_search", "slow"), _call("skills_list", "fast")), _response()])

    assert result["paused_for_update"] == up.PAUSE_REASON
    tools = _durable_tools(home)
    assert STAMP.match(tools["slow"])
    assert tools["fast"] == "the real listing"


def test_a_call_that_ignores_interrupts_is_abandoned_with_the_stamp(home, tmp_path, monkeypatch):
    """A tool that never polls the interrupt bit is abandoned by the sequential runner (the Stop path)."""
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "long_job")
    _request_pause(home)
    release = threading.Event()
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: release.wait(TOOL_CAP_S) and "late")
    try:
        result = _run_turn(agent, _session(agent, home), [_response(_call("long_job", "call_1")), _response()])
    finally:
        release.set()
    assert result["paused_for_update"] == up.PAUSE_REASON
    assert STAMP.match(_durable_tools(home)["call_1"])


def test_request_withdrawn_before_the_boundary_lets_the_turn_continue(home, tmp_path, monkeypatch):
    """The cancel landed but the window ended before the boundary: the boundary hook declines, the cancel's
    interrupt is cleared and the turn goes on (the model sees the stamped result and may re-run the call)."""
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "long_job")
    _request_pause(home)

    def _withdrawn_on_cancel(*_a, **_k):
        out = _long_call()
        af.clear_pause_request(home)  # the drain window ends while the cancelled call unwinds
        return out

    monkeypatch.setattr("model_tools.handle_function_call", _withdrawn_on_cancel)
    result = _run_turn(agent, _session(agent, home), [_response(_call("long_job", "call_1")), _response()])
    assert STAMP.match(_durable_tools(home)["call_1"])
    assert agent.client.chat.completions.create.call_count == 2
    assert result["completed"] is True and not result.get("paused_for_update")
    assert "reason" not in read_turn_marker(home, "session-key")


def test_turn_start_drops_an_earlier_turns_in_flight_calls(home, tmp_path):
    """An abandoned worker of an earlier turn never reports finished: a new turn must not count it as running
    (the watcher would cancel the new turn's model call)."""
    agent = _make_agent(tmp_path, "long_job")
    vars(agent)["_update_pause_in_flight"] = {"old": 0.0}
    agent.client.chat.completions.create.side_effect = [_response()]
    assert agent.run_conversation("hi")["completed"] is True
    assert update_pause_cancel.in_flight(agent) == {}


def test_no_tool_in_flight_still_pauses_at_the_boundary_with_real_results(home, tmp_path, monkeypatch):
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "quick")
    _request_pause(home)
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: "quick result")
    result = _run_turn(agent, _session(agent, home), [_response(_call("quick", "call_1")), _response()])
    assert result["paused_for_update"] == up.PAUSE_REASON
    assert _durable_tools(home)["call_1"] == "quick result"
    assert agent.client.chat.completions.create.call_count == 1


def test_non_resumable_turn_keeps_its_running_call(home, tmp_path, monkeypatch):
    cfg = {"desktop": {"auto_continue": dict(CFG["desktop"]["auto_continue"], resume_on_startup=False)}}
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.05)
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "long_job")
    _request_pause(home)

    def _one_second(*_a, **_k):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if is_interrupted():
                return "interrupted"
            time.sleep(0.02)
        return "ran to the end"

    monkeypatch.setattr("model_tools.handle_function_call", _one_second)
    result = _run_turn(agent, _session(agent, home), [_response(_call("long_job", "call_1")), _response()])
    assert _durable_tools(home)["call_1"] == "ran to the end"
    assert not result.get("paused_for_update") and result["completed"] is True
    assert "reason" not in read_turn_marker(home, "session-key")


# ── The watcher's poll, unit by unit ─────────────────────────────────────────────────────────────────


class _Agent:
    def __init__(self):
        self._interrupt_requested = False
        self.interrupts = []

    def interrupt(self, message=None, *, tool_reason=None, **_kw):
        self.interrupts.append(tool_reason)
        self._interrupt_requested = True


def _step(home, agent, state, monkeypatch, now):
    monkeypatch.setattr(up, "_mono", lambda: now)
    return up.mid_tool_cancel_step(_session(agent, home), "session-key", agent, state)


def test_grace_counts_from_the_later_of_sighting_and_call_start(home, monkeypatch):
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 30.0)
    record_turn_start(home, "session-key", "p")
    agent, state = _Agent(), {}
    vars(agent)["_update_pause_in_flight"] = {"c1": 100.0}
    assert _step(home, agent, state, monkeypatch, 150.0) is None  # no request yet
    _request_pause(home)
    assert _step(home, agent, state, monkeypatch, 160.0) is None  # first sighting at 160
    assert _step(home, agent, state, monkeypatch, 189.9) is None
    assert _step(home, agent, state, monkeypatch, 190.0) == ["c1"]
    assert agent.interrupts == [update_pause_cancel.CANCEL_TOOL_REASON]

    agent2, state2 = _Agent(), {}
    vars(agent2)["_update_pause_in_flight"] = {"c2": 200.0}
    assert _step(home, agent2, state2, monkeypatch, 150.0) is None  # sighted before the call began
    assert _step(home, agent2, state2, monkeypatch, 229.9) is None
    assert _step(home, agent2, state2, monkeypatch, 230.0) == ["c2"]


def test_withdrawn_request_restarts_the_grace(home, monkeypatch):
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 30.0)
    record_turn_start(home, "session-key", "p")
    agent, state = _Agent(), {}
    vars(agent)["_update_pause_in_flight"] = {"c1": 0.0}
    _request_pause(home)
    assert _step(home, agent, state, monkeypatch, 10.0) is None
    af.clear_pause_request(home)
    assert _step(home, agent, state, monkeypatch, 20.0) is None
    _request_pause(home)
    assert _step(home, agent, state, monkeypatch, 45.0) is None  # sighted again at 45
    assert _step(home, agent, state, monkeypatch, 75.0) == ["c1"]


def test_poll_never_cancels_without_a_running_call_or_over_a_pending_stop(home, monkeypatch):
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.0)
    record_turn_start(home, "session-key", "p")
    _request_pause(home)
    idle = _Agent()
    assert _step(home, idle, {}, monkeypatch, 1e6) is None and idle.interrupts == []
    stopped = _Agent()
    stopped._interrupt_requested = True  # a user Stop is already pending: it wins
    vars(stopped)["_update_pause_in_flight"] = {"c1": 0.0}
    assert _step(home, stopped, {}, monkeypatch, 1e6) is None and stopped.interrupts == []
    assert "_update_pause_cancel" not in vars(stopped)


def test_watcher_cancels_once_per_request_and_stops_on_request(home, monkeypatch):
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.0)
    record_turn_start(home, "session-key", "p")
    _request_pause(home)
    agent = _Agent()
    update_pause_cancel.started(agent, "c1")
    stop = up.start_mid_tool_cancel(_session(agent, home), "session-key", agent)
    try:
        deadline = time.monotonic() + 5
        while not agent.interrupts and time.monotonic() < deadline:
            time.sleep(0.01)
        assert agent.interrupts == [update_pause_cancel.CANCEL_TOOL_REASON]
        agent._interrupt_requested = False  # the boundary consumed it; the call still looks in flight
        time.sleep(0.3)  # health: allow HX013 -- six polls must pass without a second cancel
        assert len(agent.interrupts) == 1  # one cancel per request
    finally:
        stop()
    idle = _Agent()
    update_pause_cancel.started(idle, "c1")
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.2)
    up.start_mid_tool_cancel(_session(idle, home), "session-key", idle)()  # stopped at once (turn ended)
    time.sleep(0.4)  # health: allow HX013 -- a stopped watcher must stay silent past its grace
    assert idle.interrupts == []


def test_watcher_never_runs_on_the_turn_thread(home, monkeypatch):
    """Harnesses run turn threads inline by patching threading.Thread; the watcher must still run apart
    (an inline watcher loops on the turn's own thread until the turn ends, which it then never does)."""
    monkeypatch.setattr("threading.Thread", _InlineThread)
    agent = _Agent()
    started = time.monotonic()
    stop = up.start_mid_tool_cancel(_session(agent, home), "session-key", agent)
    stop()
    assert time.monotonic() - started < 1.0


def test_a_committed_call_no_longer_counts_as_running():
    """A worker abandoned on a timeout never reports finished; once its result is committed the watcher
    must not see it as running, or a later request would cut the turn's model call instead."""
    agent = _Agent()
    update_pause_cancel.started(agent, "stuck")
    assert update_pause_cancel.committed(agent, "stuck", "timed out") == ("timed out", False)
    assert update_pause_cancel.in_flight(agent) == {}
    assert update_pause_cancel.cancel(agent, "x") is None and agent.interrupts == []


def test_a_user_stop_is_never_relabelled_by_the_cancel(tmp_path):
    from agent.interrupt_control import interrupt_issuer
    agent = _make_agent(tmp_path, "long_job")
    agent._turn_user_intervened = False
    agent.interrupt()  # the user's Stop lands first (between cancel's check and its interrupt)
    agent.interrupt(tool_reason=update_pause_cancel.CANCEL_TOOL_REASON)
    assert interrupt_issuer(agent) != "hermes_update_pause"
    vars(agent)["_update_pause_cancel"] = {"calls": frozenset({"c1"}), "text": "stamp"}
    assert update_pause_cancel.committed(agent, "c1", "own result") == ("own result", False)  # Stop's result
    assert update_pause_cancel.take_pending(agent) is False  # ends as the Stop, not a pause
    agent.clear_interrupt()
    agent.interrupt("actually, do X instead")  # a pending redirect keeps its message too
    agent.interrupt(tool_reason=update_pause_cancel.CANCEL_TOOL_REASON)
    assert agent._interrupt_message == "actually, do X instead"


def test_the_cancel_is_not_a_user_intervention(tmp_path):
    from agent.interrupt_control import interrupt_issuer
    agent = _make_agent(tmp_path, "long_job")
    agent._turn_user_intervened = False
    update_pause_cancel.started(agent, "c1")
    assert update_pause_cancel.cancel(agent, "stamp") == ["c1"]
    assert interrupt_issuer(agent) == "hermes_update_pause"
    assert agent._turn_user_intervened is False
    assert update_pause_cancel.committed(agent, "c1", "partial") == ("stamp", True)


def test_a_foreground_command_handed_to_the_background_keeps_its_result(tmp_path):
    """terminal.update_survivable_processes: the cancel did not kill the command, it became a job. Its result
    (``process continues as <id>``) is kept, so the resumed turn sees the job instead of "re-run"."""
    import json

    agent = _make_agent(tmp_path, "long_job")
    update_pause_cancel.started(agent, "c1")
    assert update_pause_cancel.cancel(agent, "stamp") == ["c1"]
    survived = json.dumps({"output": "[interrupted by hermes update pause]\nprocess continues as proc_1",
                           "exit_code": None, "status": "continues_in_background", "session_id": "proc_1"})
    assert update_pause_cancel.committed(agent, "c1", survived) == (survived, True)


@pytest.mark.parametrize("result", [
    None, {"status": "continues_in_background", "session_id": "proc_1"},  # not the tool's JSON text
    'output mentions "continues_in_background" but is not JSON',
    '{"status": "completed", "note": "continues_in_background"}',
    '["continues_in_background"]',
    '{"status": "continues_in_background", "session_id": ""}',
])
def test_any_other_result_of_a_cancelled_call_is_stamped(tmp_path, result):
    agent = _make_agent(tmp_path, "long_job")
    update_pause_cancel.started(agent, "c1")
    assert update_pause_cancel.cancel(agent, "stamp") == ["c1"]
    assert update_pause_cancel.committed(agent, "c1", result) == ("stamp", True)


def test_a_later_request_in_the_same_turn_cancels_again(home, monkeypatch):
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 30.0)
    record_turn_start(home, "session-key", "p")
    agent, state = _Agent(), {}
    update_pause_cancel.started(agent, "c1")
    vars(agent)["_update_pause_in_flight"]["c1"] = 0.0
    _request_pause(home)
    assert _step(home, agent, state, monkeypatch, 10.0) is None
    assert _step(home, agent, state, monkeypatch, 40.0) == ["c1"]
    agent._interrupt_requested = False  # the boundary consumed the cancel and declined: the turn goes on
    update_pause_cancel.take_pending(agent)
    assert _step(home, agent, state, monkeypatch, 41.0) is None  # same request: no second cancel
    af.clear_pause_request(home)
    assert _step(home, agent, state, monkeypatch, 50.0) is None
    update_pause_cancel.started(agent, "c2")
    vars(agent)["_update_pause_in_flight"]["c2"] = 55.0
    _request_pause(home)
    assert _step(home, agent, state, monkeypatch, 60.0) is None  # sighted at 60
    assert _step(home, agent, state, monkeypatch, 90.0) == ["c2"]


def test_a_stop_during_the_boundary_hook_ends_the_turn(home, tmp_path, monkeypatch):
    """The cancel's pause is declined because a user Stop landed while the hook ran: only the cancel's own
    interrupt may be cleared, so the turn ends as that Stop instead of calling the model again."""
    record_turn_start(home, "session-key", "p")
    agent = _make_agent(tmp_path, "long_job")
    session = _session(agent, home)
    _request_pause(home)
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: _long_call())
    agent.client.chat.completions.create.side_effect = [_response(_call("long_job", "call_1")), _response()]

    def _hook():  # declines (returns None) after the user's Stop is published while it runs
        agent.interrupt()

    agent.step_boundary_pause = _hook
    stop = up.start_mid_tool_cancel(session, "session-key", agent)
    try:
        result = agent.run_conversation("run the long job")
    finally:
        stop()
    assert agent.client.chat.completions.create.call_count == 1
    assert result["interrupted"] is True and not result.get("paused_for_update")


def test_clear_interrupt_only_reason_spares_a_relabelled_interrupt(tmp_path):
    agent = _make_agent(tmp_path, "long_job")
    agent.interrupt(tool_reason=update_pause_cancel.CANCEL_TOOL_REASON)
    agent.interrupt()  # a Stop relabels it
    assert agent.clear_interrupt(only_reason=update_pause_cancel.CANCEL_TOOL_REASON) is False
    assert agent._interrupt_requested is True
    agent.clear_interrupt()
    agent.interrupt(tool_reason=update_pause_cancel.CANCEL_TOOL_REASON)
    assert agent.clear_interrupt(only_reason=update_pause_cancel.CANCEL_TOOL_REASON) is True
    assert agent._interrupt_requested is False


def test_grace_counts_from_the_newest_running_call(home, monkeypatch):
    """Parallel calls: one cancel ends them all, so it waits until the newest one has had its grace."""
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 30.0)
    record_turn_start(home, "session-key", "p")
    _request_pause(home)
    agent, state = _Agent(), {}
    vars(agent)["_update_pause_in_flight"] = {"c1": 100.0, "c2": 120.0}
    assert _step(home, agent, state, monkeypatch, 100.0) is None
    assert _step(home, agent, state, monkeypatch, 149.9) is None
    assert _step(home, agent, state, monkeypatch, 150.0) == ["c1", "c2"]


def test_cancel_with_nothing_running_does_not_interrupt():
    """``cancel`` itself refuses an idle turn: an interrupt then would cut the model call instead."""
    agent = _Agent()
    assert update_pause_cancel.cancel(agent, "x") is None
    assert agent.interrupts == [] and "_update_pause_cancel" not in vars(agent)
    update_pause_cancel.started(agent, "c1")
    update_pause_cancel.finished(agent, "c1")
    assert update_pause_cancel.cancel(agent, "x") is None and agent.interrupts == []


@pytest.mark.parametrize("case", ["resume_on_startup_off", "bot_room", "opted_out", "no_marker"])
def test_poll_leaves_a_non_resumable_turn_alone(home, monkeypatch, case):
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.0)
    cfg = {"desktop": {"auto_continue": dict(CFG["desktop"]["auto_continue"])}}
    if case == "resume_on_startup_off":
        cfg["desktop"]["auto_continue"]["resume_on_startup"] = False
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    if case != "no_marker":
        record_turn_start(home, "session-key", "p", auto_continue=case != "opted_out")
    _request_pause(home)
    agent = _Agent()
    vars(agent)["_update_pause_in_flight"] = {"c1": 0.0}
    session = dict(_session(agent, home), **({"source": "bot_room"} if case == "bot_room" else {}))
    monkeypatch.setattr(up, "_mono", lambda: 1e6)
    assert up.mid_tool_cancel_step(session, "session-key", agent, {}) is None
    assert agent.interrupts == []


def test_a_stop_after_the_cancel_ends_the_turn_as_that_stop(home):
    """The cancel's record is consumed at the boundary; a later user Stop relabels the interrupt and wins."""
    agent = types.SimpleNamespace(_interrupt_requested=True, _tool_interrupt_reason="explicit stop requested")
    vars(agent)["_update_pause_cancel"] = {"calls": frozenset({"c1"}), "text": "x"}
    vars(agent)["_update_pause_in_flight"] = {"c1": 0.0, "c2": 1.0}
    assert update_pause_cancel.take_pending(agent) is False
    assert update_pause_cancel.in_flight(agent) == {"c2": 1.0}  # the abandoned cancelled call is dropped


def test_gateway_turn_starts_and_stops_the_watcher(monkeypatch, home):
    """prompt_turn._invoke_agent installs the watcher next to the boundary hook and stops it at turn end."""
    calls = []

    def _start(session, key, agent):
        calls.append(("start", key))
        return lambda: calls.append(("stop", key))

    monkeypatch.setattr(up, "start_mid_tool_cancel", _start)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    for name, value in {"_emit": lambda *a, **k: None, "_wire_callbacks": lambda sid: None,
                        "_sync_agent_model_with_config": lambda sid, s: None,
                        "_session_cwd": lambda s: str(home), "_register_session_cwd": lambda s: None,
                        "_tts_stream_begin": lambda: None, "_sync_session_key_after_compress": lambda *a, **k: None,
                        "_get_usage": lambda a: {}}.items():
        monkeypatch.setattr(server, name, value)

    def _run(message, **kwargs):
        calls.append(("run",))
        return {"final_response": "done", "messages": []}

    agent = types.SimpleNamespace(session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None)
    session = {"agent": agent, "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
               "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
               "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
               "profile_home": str(home)}
    server._run_prompt_submit("rid", "ui-sid", session, "go")
    assert calls == [("start", "session-key"), ("run",), ("stop", "session-key")]
