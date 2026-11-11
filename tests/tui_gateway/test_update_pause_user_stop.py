"""[daveey] t_01a1c895eb0596b2: a user Stop beats the core-update pause of the turn it stops.

A Stop that landed while the pause hook ran (and succeeded) ended the turn as stopped, but the hook had
already stamped the interrupted-turn marker ``reason: core_update_pause`` and told the session to keep it,
so the restarted backend's startup sweep continued a turn the user had stopped. The same held for a Stop
right after the hook returned, and for both ways a pause lands: at a step boundary and after a mid-tool
cancel.

These drive the gateway's real turn (``_run_prompt_submit`` on its own run thread) with a real ``AIAgent``,
the real hook and mid-tool watcher, and the real ``session.interrupt`` RPC as the Stop; only the model and
the tool bodies are fakes. The fresh backend is the real startup sweep over the marker file.
"""

from __future__ import annotations

import json
import threading
import types

import pytest

from hermes_cli import admission_fence as af
from tests.tui_gateway import test_update_pause_mid_tool as mt
from tui_gateway import server
from tui_gateway import startup_resume as sr
from tui_gateway import update_pause as up
from tui_gateway.contracts.registry import check_payload
from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker, record_turn_start

CFG = mt.CFG
SID = "ui-sid"
KEY = "session-key"
home = mt.home
_quiet = mt._quiet


@pytest.fixture()
def env(monkeypatch, tmp_path, home):
    frames: list = []
    watchers: list = []

    def _emit(event, sid, payload=None, *a, **k):
        check_payload(event, payload)
        frames.append((event, payload))

    for name, value in {"_emit": _emit, "_wire_callbacks": lambda sid: None,
                        "_sync_agent_model_with_config": lambda sid, s: None,
                        "_session_cwd": lambda s: str(tmp_path), "_register_session_cwd": lambda s: None,
                        "_tts_stream_begin": lambda: None, "_sync_session_key_after_compress": lambda *a, **k: None,
                        "_get_usage": lambda a: {}}.items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(up, "start_in_place_watcher", lambda sid, session, key: watchers.append((sid, key)))
    yield types.SimpleNamespace(frames=frames, watchers=watchers, home=home, tmp_path=tmp_path)
    server._sessions.pop(SID, None)


def _user_stop():
    """The Desktop's Stop button: the real ``session.interrupt`` RPC."""
    reply = server._methods["session.interrupt"]("rid-stop", {"session_id": SID, "origin": "stop_button"})
    assert reply.get("result", {}).get("status") == "interrupted", reply


def _stop_while_the_hook_runs(monkeypatch):
    """The Stop lands inside the hook, after its last marker write and before it returns its verdict."""
    real, fired = up._drainer_lock, []

    def _drainer_lock(session):
        if not fired:
            fired.append(1)
            _user_stop()
        return real(session)

    monkeypatch.setattr(up, "_drainer_lock", _drainer_lock)
    return fired


def _stop_right_after_the_hook(monkeypatch):
    """The Stop lands after the hook returned its pause verdict, before the agent loop acts on it."""
    real, fired = up.boundary_pause_hook, []

    def _boundary_pause_hook(session, marker_key):
        hook = real(session, marker_key)

        def _hook():
            verdict = hook()
            if verdict and not fired:
                fired.append(verdict)
                _user_stop()
            return verdict

        return _hook

    monkeypatch.setattr(up, "boundary_pause_hook", _boundary_pause_hook)
    return fired


def _stop_during_payload(monkeypatch):
    """Stop after payload construction has begun, before terminal publication."""
    real, fired = server._complete_turn_payload, []

    def complete(*args, **kwargs):
        payload = real(*args, **kwargs)
        fired.append(1)
        _user_stop()
        return payload

    monkeypatch.setattr(server, "_complete_turn_payload", complete)
    return fired


STOPS = {"during_hook": _stop_while_the_hook_runs, "after_hook": _stop_right_after_the_hook,
         "during_payload": _stop_during_payload}


def _turn(env, monkeypatch, path):
    """One gateway turn that meets a pause request: ``boundary`` (a quick tool call, the pause lands at the
    step boundary after it) or ``mid_tool`` (a long call the watcher cancels after the grace)."""
    agent = mt._make_agent(env.tmp_path, "job")

    def _tool(name, *a, **k):
        mt._request_pause(env.home)  # the drain window asks while the turn is mid-step (admission is past)
        return mt._long_call() if path == "mid_tool" else "quick result"

    monkeypatch.setattr("model_tools.handle_function_call", _tool)
    agent.client.chat.completions.create.side_effect = [mt._response(mt._call("job", "call_1")), mt._response(),
                                                        mt._response()]
    session = {"agent": agent, "session_key": KEY, "history": [], "history_lock": threading.RLock(),
               "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
               "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
               "profile_home": str(env.home)}
    server._sessions[SID] = session
    server._run_prompt_submit("rid", SID, session, "run the long job")
    thread = session.get("_run_thread")
    if thread is not None:
        thread.join(60)
        assert not thread.is_alive()
    return session, agent


def _backend_exits(home):
    """The update restarts the backend: the marker's writer is gone."""
    path = home / "desktop" / "interrupted_turns.json"
    if not path.exists():
        return
    entries = json.loads(path.read_text())
    for entry in entries.values():
        entry["writer_pid"] = 2 ** 22 - 3
        entry.pop("writer_start_time", None)
    path.write_text(json.dumps(entries))


def _fresh_backend_continues(env, monkeypatch) -> list[str]:
    """What a fresh backend's startup sweep submits for the session."""
    _backend_exits(env.home)
    af.clear_pause_request(env.home)
    af.clear_hold(env.home)
    pending = [r["session_key"] for r in sr.pending_resumes(env.home, CFG)]
    submitted: list[str] = []
    monkeypatch.setattr(server.threading, "Thread", mt._InlineThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, s: None)
    monkeypatch.setattr(server, "_wait_agent", lambda s, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, s, text, **kw: submitted.append(text))
    fresh = {"agent": types.SimpleNamespace(), "session_key": KEY, "history": [],
             "history_lock": threading.RLock(), "running": False}
    scheduled = server._maybe_schedule_auto_continue("fresh-sid", fresh, KEY)
    assert (scheduled is not None) == bool(submitted) == bool(pending)
    return submitted


def _complete(env) -> dict:
    (payload,) = [p for e, p in env.frames if e == "message.complete"]
    return payload


@pytest.mark.parametrize("stop", sorted(STOPS))
@pytest.mark.parametrize("path", ["boundary", "mid_tool"])
def test_a_user_stop_around_a_successful_pause_hook_is_not_continued_after_the_restart(env, monkeypatch, path, stop):
    fired = STOPS[stop](monkeypatch)
    session, agent = _turn(env, monkeypatch, path)

    assert fired  # the Stop really landed around a pause hook that had stamped the marker
    assert agent.client.chat.completions.create.call_count == 1  # the stopped turn never calls the model again
    complete = _complete(env)
    assert complete["status"] == "interrupted" and not complete.get("paused_for_update")
    assert up.PAUSE_STATUS_TEXT not in (complete.get("text") or "")  # never announced as "continues automatically"
    assert env.watchers == []  # nothing is left to continue it in place either
    assert not session.get("_update_pause_marker_key")
    assert read_turn_marker(env.home, KEY) is None
    assert _fresh_backend_continues(env, monkeypatch) == []


@pytest.mark.parametrize("path", ["boundary", "mid_tool"])
def test_a_pause_with_no_stop_still_resumes_after_the_restart(env, monkeypatch, path):
    session, agent = _turn(env, monkeypatch, path)

    assert agent.client.chat.completions.create.call_count == 1
    complete = _complete(env)
    assert complete.get("paused_for_update") is True and up.PAUSE_STATUS_TEXT in complete["text"]
    assert env.watchers == [(SID, KEY)]
    assert session["_update_pause_marker_key"] == KEY
    marker = read_turn_marker(env.home, KEY)
    assert marker["reason"] == up.PAUSE_REASON and marker["pauses"] == 1 and marker["attempts"] == 0
    assert _fresh_backend_continues(env, monkeypatch) == [up.pause_note("run the long job")]


def test_an_idle_stop_after_the_pause_keeps_the_paused_turn(env, monkeypatch):
    """Unchanged: once the turn has ended as paused, a Stop on the idle chat stops nothing and the paused
    request still continues (only a Stop of the running turn beats its pause)."""
    session, _agent = _turn(env, monkeypatch, "boundary")
    assert session["running"] is False
    _user_stop()
    assert session["_update_pause_marker_key"] == KEY
    assert _fresh_backend_continues(env, monkeypatch) == [up.pause_note("run the long job")]


# ── The pieces, one condition each ───────────────────────────────────────────────────────────────────


class _Agent:
    def __init__(self, reason=None):
        self._interrupt_requested = reason is not None
        self._tool_interrupt_reason = reason


def _paused_session(home, agent=None, **extra):
    record_turn_start(home, KEY, "p")
    assert mark_turn_paused(home, KEY, up.PAUSE_REASON)
    return {"agent": agent or _Agent(), "session_key": KEY, "profile_home": str(home),
            "history_lock": threading.RLock(), "_update_pause_marker_key": KEY, "_active_turn_marker_key": KEY,
            **extra}


@pytest.mark.parametrize("reason, stopped", [
    ("explicit stop requested", True), ("user interrupt", True), ("user sent a new message", True),
    (None, False), ("hermes update pause", False), ("backend exit", False), ("lease takeover", False)])
def test_only_a_pending_user_interrupt_counts_as_a_stop(reason, stopped):
    assert up.user_stop_pending({"agent": _Agent(reason)}) is stopped
    assert up.user_stop_pending({}) is False


@pytest.mark.parametrize("reason, paused", [("explicit stop requested", None), ("backend exit", up.PAUSE_REASON),
                                            (None, up.PAUSE_REASON)])
def test_the_hook_declines_only_for_a_user_stop(home, reason, paused):
    """A backend exit's interrupt (or none) leaves the pause standing: that turn is still continued."""
    record_turn_start(home, KEY, "p")
    mt._request_pause(home)
    session = {"agent": _Agent(reason), "session_key": KEY, "profile_home": str(home),
               "history_lock": threading.RLock()}
    assert up.boundary_pause_hook(session, KEY)() == paused
    assert session.get("_update_pause_marker_key") == (KEY if paused else None)


def test_a_stop_pending_before_the_hook_stamps_nothing_and_stops_no_job(home, monkeypatch):
    record_turn_start(home, KEY, "p")
    mt._request_pause(home)
    monkeypatch.setattr(up, "stop_owned_jobs_for_update_pause", lambda *a, **k: pytest.fail("stopped a job"))
    session = {"agent": _Agent("explicit stop requested"), "session_key": KEY, "profile_home": str(home),
               "history_lock": threading.RLock()}
    assert up.boundary_pause_hook(session, KEY)() is None
    marker = read_turn_marker(home, KEY)
    assert marker.get("reason") != up.PAUSE_REASON and not marker.get("pauses")


def test_a_stop_of_the_running_turn_drops_its_pause(home):
    session = _paused_session(home, running=True)
    assert up.user_stop_wins(session) is True
    assert "_update_pause_marker_key" not in session
    server._retire_turn_marker(session, KEY)  # what the Stop (or the turn's end) does next
    assert read_turn_marker(home, KEY) is None


def test_a_stop_never_drops_another_turns_pause(home):
    """The paused turn ended and a synthesized turn (no marker of its own) runs over its marker: stopping
    that turn must not drop the paused request."""
    session = _paused_session(home, running=True)
    session.pop("_active_turn_marker_key")
    assert up.user_stop_wins(session) is False
    assert session["_update_pause_marker_key"] == KEY
    server._retire_turn_marker(session, KEY)
    assert read_turn_marker(home, KEY)["reason"] == up.PAUSE_REASON


def test_pause_stands_only_while_the_session_keeps_this_turns_marker(home):
    paused = {"paused_for_update": up.PAUSE_REASON}
    session = _paused_session(home)
    assert up.pause_stands(session, KEY, paused) is True
    assert up.pause_stands(session, "other-key", paused) is False
    assert session["_update_pause_marker_key"] == KEY
    assert up.pause_stands(_paused_session(home), KEY, {"interrupted": True}) is False
    assert up.pause_stands(_paused_session(home), KEY, None) is False
    session.pop("_update_pause_marker_key")  # a Stop that won dropped it
    assert up.pause_stands(session, KEY, paused) is False


def test_a_backend_exit_keeps_the_pause_of_a_turn_it_interrupted(home):
    """Unchanged: the exit's own interrupt is not a Stop, so the paused turn is still continued."""
    session = _paused_session(home, _exit_keeps_turn_marker=True)
    assert up.pause_stands(session, KEY, {"interrupted": True}) is False
    assert session["_update_pause_marker_key"] == KEY
    server._retire_turn_marker(session, KEY)
    assert read_turn_marker(home, KEY)["reason"] == up.PAUSE_REASON


def test_a_turn_that_did_not_end_paused_does_not_keep_the_pause_marker(home):
    """The hook paid out but the turn ended otherwise (a Stop won in the agent loop): the turn's end drops
    the session's claim so its marker retires like any stopped turn's."""
    session = _paused_session(home)
    assert up.pause_stands(session, KEY, {"interrupted": True}) is False
    assert "_update_pause_marker_key" not in session
    other = _paused_session(home, _update_pause_marker_key="earlier-turn")
    assert up.pause_stands(other, KEY, {"interrupted": True}) is False
    assert other["_update_pause_marker_key"] == "earlier-turn"  # not this turn's claim to drop
