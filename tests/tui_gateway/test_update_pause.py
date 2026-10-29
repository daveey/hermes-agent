"""[daveey] Core-update pause: a Desktop turn the restarted backend can auto-continue pauses at a step
boundary when hermes-sync's drain window asks (``<root>/core-update/pause-request``), so a long turn no
longer holds an update for a whole window. The turn keeps its interrupted-turn marker (``reason:
core_update_pause``), releases its admission reservation, and the next backend continues it with a note
that does not use up a crash-resume attempt. Turns that cannot be auto-continued are untouched.
"""

from __future__ import annotations

import os
import threading
import time
import types

import pytest

from hermes_cli import admission_fence as af
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.contracts.events import MessageCompletePayload
from tui_gateway.contracts.registry import STRICT, check_payload
from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker, record_turn_start

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}


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


def _session(agent=None, **extra):
    return {"agent": agent if agent is not None else types.SimpleNamespace(), "session_key": "session-key",
            "history": [], "history_lock": threading.RLock(), "history_version": 0, "running": False,
            "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None,
            "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None, **extra}


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    return tmp_path


def _request_pause(root):
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 120)


# ── The boundary hook ────────────────────────────────────────────────────────────────────────────────


def test_no_request_no_pause(home):
    record_turn_start(home, "session-key", "the prompt")
    assert up.boundary_pause_hook(_session(profile_home=str(home)), "session-key")() is None
    assert "reason" not in read_turn_marker(home, "session-key")


def test_resumable_turn_pauses_and_its_marker_survives_the_turn_end(home):
    record_turn_start(home, "session-key", "the prompt")
    session = _session(profile_home=str(home))
    _request_pause(home)

    assert up.boundary_pause_hook(session, "session-key")() == up.PAUSE_REASON

    marker = read_turn_marker(home, "session-key")
    assert marker["auto_continue"] is True
    assert marker["reason"] == "core_update_pause"
    assert marker["pauses"] == 1 and marker["attempts"] == 0
    assert marker["prompt"] == "the prompt"
    # The paused turn's terminal frame must not retire the marker: the next backend continues from it.
    server._retire_turn_marker(session, "session-key")
    assert read_turn_marker(home, "session-key") is not None


@pytest.mark.parametrize("case", ["resume_on_startup_off", "auto_continue_disabled", "bot_room", "opted_out",
                                  "attempts_used_up", "stale", "no_marker"])
def test_turn_that_cannot_be_auto_continued_is_untouched(home, monkeypatch, case):
    cfg = {"desktop": {"auto_continue": dict(CFG["desktop"]["auto_continue"])}}
    extra = {}
    if case == "resume_on_startup_off":
        cfg["desktop"]["auto_continue"]["resume_on_startup"] = False
    if case == "auto_continue_disabled":
        cfg["desktop"]["auto_continue"]["enabled"] = False
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    if case == "bot_room":
        extra["source"] = "bot_room"
    if case != "no_marker":
        record_turn_start(home, "session-key", "the prompt", auto_continue=case != "opted_out",
                          attempts=2 if case == "attempts_used_up" else 0)
    if case == "stale":
        later = time.time() + 4 * 3600  # past the 240-min freshness window
        monkeypatch.setattr(up, "_now", lambda: later)
    _request_pause(home)

    assert up.boundary_pause_hook(_session(profile_home=str(home), **extra), "session-key")() is None
    marker = read_turn_marker(home, "session-key")
    assert marker is None or "reason" not in marker


# ── The whole turn ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def turn_env(monkeypatch, tmp_path, home):
    frames: list = []
    watchers: list = []
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    def _emit(event, sid, payload=None, *a, **k):
        # The real wire contract checks every frame (strict under the test suite): replacing ``_emit``
        # with a bare recorder once hid a terminal frame MessageCompletePayload rejects.
        check_payload(event, payload)
        frames.append((event, payload))

    monkeypatch.setattr(server, "_emit", _emit)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(up, "start_in_place_watcher", lambda sid, session, key: watchers.append((sid, key)))
    return types.SimpleNamespace(frames=frames, watchers=watchers)


def test_paused_turn_releases_its_reservation_keeps_its_marker_and_says_so(turn_env, home):
    seen = {}

    def _run(message, **kwargs):
        seen["reservations_in_turn"] = af.state(home)["reservations"]
        _request_pause(home)  # the drain window asks while the turn is mid-step
        reason = agent.step_boundary_pause()
        return {"final_response": "", "messages": [], "interrupted": True, "completed": False,
                "paused_for_update": reason}

    agent = types.SimpleNamespace(session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None)
    session = _session(agent=agent, running=True, profile_home=str(home))
    server._run_prompt_submit("rid", "ui-sid", session, "the prompt")

    assert seen["reservations_in_turn"] == 1
    assert af.state(home)["reservations"] == 0  # the drain can apply
    marker = read_turn_marker(home, "session-key")
    assert marker is not None and marker["reason"] == "core_update_pause" and marker["auto_continue"] is True
    statuses = [p.get("text") for e, p in turn_env.frames if e == "status.update" and p]
    assert up.PAUSE_STATUS_TEXT in statuses
    (complete,) = [p for e, p in turn_env.frames if e == "message.complete"]
    assert up.PAUSE_STATUS_TEXT in complete["text"]
    assert session["running"] is False
    assert agent.step_boundary_pause is None  # per-turn hook, never left on the cached agent
    assert turn_env.watchers == [("ui-sid", "session-key")]


def test_announce_pause_frame_satisfies_the_message_complete_contract(monkeypatch):
    """The producer's own output, not a hand-built dict: ``announce_pause`` stamps the deliberate
    ``paused_for_update`` signal on the terminal frame, and the declared contract accepts it."""
    assert STRICT  # the suite's strict mode is what turns a violation into a failure
    emitted: list = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, p=None, *a, **k: emitted.append((event, p)))
    payload = {"text": "partial answer", "status": "interrupted", "rendered": "<p>partial answer</p>"}
    up.announce_pause("ui-sid", payload)
    assert payload["paused_for_update"] is True and "rendered" not in payload
    check_payload("message.complete", payload)
    assert MessageCompletePayload.model_validate(payload).paused_for_update is True
    assert [e for e, _ in emitted] == ["status.update"]
    check_payload("status.update", emitted[0][1])


def test_ordinary_stop_is_not_reported_as_a_pause(turn_env, home):
    """An interrupted turn the update did not pause keeps the plain terminal frame: no pause flag,
    no pause text, no in-place watcher."""
    def _run(message, **kwargs):
        return {"final_response": "", "messages": [], "interrupted": True, "completed": False}

    agent = types.SimpleNamespace(session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None)
    server._run_prompt_submit("rid", "ui-sid", _session(agent=agent, running=True, profile_home=str(home)), "go")
    (complete,) = [p for e, p in turn_env.frames if e == "message.complete"]
    assert "paused_for_update" not in complete
    assert up.PAUSE_STATUS_TEXT not in str(complete.get("text"))
    assert turn_env.watchers == []


def test_turn_without_a_request_ends_normally(turn_env, home):
    def _run(message, **kwargs):
        assert agent.step_boundary_pause() is None
        return {"final_response": "done", "messages": []}

    agent = types.SimpleNamespace(session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None)
    server._run_prompt_submit("rid", "ui-sid", _session(agent=agent, running=True, profile_home=str(home)), "go")
    assert read_turn_marker(home, "session-key") is None
    assert turn_env.watchers == []


def test_next_user_turn_retires_its_own_marker_again(turn_env, home):
    """The pause's keep-the-marker flag belongs to the paused turn only."""
    session = _session(running=True, profile_home=str(home))
    calls = {"n": 0}

    def _run(message, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _request_pause(home)
            return {"final_response": "", "messages": [], "interrupted": True,
                    "paused_for_update": session["agent"].step_boundary_pause()}
        return {"final_response": "done", "messages": []}

    session["agent"] = types.SimpleNamespace(session_id="session-key", run_conversation=_run,
                                             clear_interrupt=lambda: None)
    server._run_prompt_submit("rid", "ui-sid", session, "first")
    af.clear_pause_request(home)
    af.clear_hold(home)
    session["running"] = True
    server._run_prompt_submit("rid", "ui-sid", session, "second")
    assert read_turn_marker(home, "session-key") is None


def test_continuation_turn_carries_the_pause_count_not_an_attempt(turn_env, home):
    seen = []

    def _run(message, **kwargs):
        seen.append(read_turn_marker(home, "session-key"))
        return {"final_response": "done", "messages": []}

    agent = types.SimpleNamespace(session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None)
    session = _session(agent=agent, running=True, profile_home=str(home), _auto_continue_attempt=0,
                       _auto_continue_pauses=1, _auto_continue_prompt="the original prompt")
    server._run_prompt_submit("rid", "ui-sid", session, up.pause_note("the original prompt"))
    (marker,) = seen
    assert (marker["attempts"], marker["pauses"], marker["prompt"]) == (0, 1, "the original prompt")
    assert "reason" not in marker
    assert "_auto_continue_pauses" not in session


# ── Resume on the new backend ────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def schedule_env(monkeypatch, home):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    submitted: list = []
    monkeypatch.setattr(server, "_run_prompt_submit",
                        lambda rid, sid, session, text, **kw: submitted.append((text, kw)))
    return submitted


def test_paused_turn_continues_with_the_pause_note_and_no_attempt_used(schedule_env, home):
    record_turn_start(home, "session-key", "fix the flaky test", attempts=1)
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    session = _session()

    result = server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert result is not None and result["attempt"] == 1  # unchanged: a pause is not a crash
    assert session["_auto_continue_attempt"] == 1
    assert session["_auto_continue_pauses"] == 1
    (text, kwargs), = schedule_env
    assert text == up.pause_note("fix the flaky test")
    assert "paused at a step boundary" in text and "every tool result above is complete" in text.lower()
    assert "fix the flaky test" in text
    assert kwargs["display_kind"] == "auto_continue"


def test_crash_marker_still_uses_the_crash_note(schedule_env, home):
    record_turn_start(home, "session-key", "fix the flaky test")
    session = _session()
    server._maybe_schedule_auto_continue("sid", session, "session-key")
    (text, _), = schedule_env
    assert text.startswith("[System note: Your previous turn was interrupted")
    assert session["_auto_continue_attempt"] == 1


def test_startup_sweep_picks_up_a_paused_turn_of_a_dead_backend(home):
    from tui_gateway import startup_resume as sr
    import json
    record_turn_start(home, "session-key", "the prompt")
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries["session-key"]["writer_pid"] = 2 ** 22 - 3  # the pre-update backend has exited
    entries["session-key"].pop("writer_start_time", None)
    path.write_text(json.dumps(entries))
    assert [r["session_key"] for r in sr.pending_resumes(home, CFG)] == ["session-key"]


# ── A window that ends without a restart ─────────────────────────────────────────────────────────────


def test_in_place_resume_decision(home):
    record_turn_start(home, "session-key", "the prompt")
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    session = _session(profile_home=str(home))
    no_skew = lambda: None

    _request_pause(home)
    assert up.in_place_resume_action(session, "session-key", skew=no_skew) == "wait"
    af.clear_pause_request(home)
    assert up.in_place_resume_action(session, "session-key", skew=no_skew) == "wait"  # hold still up
    af.clear_hold(home)
    # The checkout moved: this backend retires once idle and the new one continues the turn.
    assert up.in_place_resume_action(session, "session-key", skew=lambda: ("a", "b")) == "leave"
    # No update happened (window expired, apply refused): continue here.
    assert up.in_place_resume_action(session, "session-key", skew=no_skew) == "resume"
    session["running"] = True  # the user sent a new message first
    assert up.in_place_resume_action(session, "session-key", skew=no_skew) == "done"
    session["running"] = False
    record_turn_start(home, "session-key", "a newer turn")
    assert up.in_place_resume_action(session, "session-key", skew=no_skew) == "done"


def test_marker_writer_is_this_process(home):
    record_turn_start(home, "session-key", "p")
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    import json
    entry = json.loads((home / "desktop" / "interrupted_turns.json").read_text())["session-key"]
    assert entry["writer_pid"] == os.getpid() and entry["paused_at"] > 0
