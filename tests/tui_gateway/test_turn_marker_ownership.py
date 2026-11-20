"""[daveey] A refused submit never clears a live turn's marker or state (hub 2026-11-20, chat 20261119_220010_0fbe00).

At 20:52:54 an auto-continue turn started; 230 ms later a second automatic submit on the same chat was refused at
``_admit_prompt_turn`` (SESSION_NOT_OWNED). That refusal emitted its terminal error with the default
``retire_marker=True`` and deleted the LIVE turn's marker (the refused submit never wrote one). The live turn then
sat in a clarify; every update-pause hook read "no turn marker", so the clarify was never withdrawn and the hub's
core update waited 30+ min on the turn's unpublished admission reservation. A cross-process variant (20:33): pid
399307 refused a turn and deleted pid 491642's marker in the shared HERMES_HOME.

These tests drive the REAL ``_run_prompt_submit`` (a live turn on its own worker thread, its marker and admission
on disk) and the real refusal paths.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import types

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tools import clarify_early
from tools.clarify_tool import clarify_tool
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

pytestmark = pytest.mark.platforms("posix")

KEY = "20261119_220010_0fbe00"
CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}
WAIT_S = 10
NOT_OWNED = types.SimpleNamespace(reason="SESSION_NOT_OWNED")
QUESTIONS = [{"question": "Which runner pool?", "choices": ["org", "personal"]}]


class LiveAgent:
    """An agent whose turn blocks until released (``body`` runs inside the turn, e.g. a clarify)."""

    def __init__(self, body=None):
        self.session_id = KEY
        self.entered = threading.Event()
        self.release = threading.Event()
        self.body = body
        self.step_boundary_pause = None

    def clear_interrupt(self):
        pass

    def run_conversation(self, message, **kw):
        self.entered.set()
        if self.body is not None:
            return self.body(self)
        self.release.wait(WAIT_S)
        return {"final_response": "done", "messages": []}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    frames: list = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None, *a, **k: frames.append((event, sid, payload)))
    for name in ("_wire_callbacks", "_register_session_cwd"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: True)
    monkeypatch.setattr(server, "_session_moved_refusal", lambda session: None)
    # The live turn passed its ownership fence; from `slot["refuse"]` on, the chat is held elsewhere.
    slot = {"refuse": None}
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session, **kw: slot["refuse"])
    watchers: list = []
    monkeypatch.setattr(up, "start_in_place_watcher", lambda sid, session, key: watchers.append(key))
    monkeypatch.setattr(clarify_early, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(up, "_clarify_provider_installed", False, raising=False)
    clarify_early.reset_for_tests()
    yield types.SimpleNamespace(home=tmp_path, frames=frames, slot=slot, watchers=watchers)
    clarify_early.reset_for_tests()
    af.clear_hold(tmp_path)


def _record(home, agent, sid):
    return {"agent": agent, "session_key": KEY, "history": [], "history_lock": threading.RLock(),
            "history_version": 0, "running": False, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
            "profile_home": str(home), "source": "desktop", "_test_sid": sid}


def _start_live_turn(env, monkeypatch, body=None):
    """The 20:52:54 auto-continue: a kickoff-style claim (running + its claim object), then the real submit."""
    agent = LiveAgent(body)
    live = _record(env.home, agent, "ui-live")
    monkeypatch.setitem(server._sessions, "ui-live", live)
    with live["history_lock"]:
        live["running"] = True
        live["_notif_turn_claim"] = object()
    assert server._run_prompt_submit("rid-live", "ui-live", live, "continue the paused turn",
                                     display_kind="auto_continue") is True
    assert agent.entered.wait(WAIT_S), "the live turn never reached the model"
    marker = read_turn_marker(env.home, KEY)
    assert marker is not None and marker["prompt"] == "continue the paused turn"
    return live, agent


def _finish(live, agent):
    agent.release.set()
    thread = live.get("_run_thread")
    if thread is not None:
        thread.join(WAIT_S)
        assert not thread.is_alive()


def _refuse_sibling_notice(env):
    """A second automatic submit on the same chat, from a sibling record, refused at ``_admit_prompt_turn``."""
    sibling = _record(env.home, types.SimpleNamespace(session_id=KEY, clear_interrupt=lambda: None), "ui-sibling")
    assert server._notif_claim_turn(sibling) is True
    env.slot["refuse"] = NOT_OWNED
    accepted = server._notif_submit("rid-notice", "ui-sibling", sibling, "kanban notice", "kanban dispatch failed")
    assert accepted is False
    return sibling


# --- T1: the incident --------------------------------------------------------------------------------------------

def test_refused_sibling_submit_keeps_the_live_turns_marker(env, monkeypatch, caplog):
    live, agent = _start_live_turn(env, monkeypatch)
    try:
        caplog.set_level(logging.INFO)
        sibling = _refuse_sibling_notice(env)
        assert any("Refusing turn for session" in r.getMessage() and "SESSION_NOT_OWNED" in r.getMessage()
                   for r in caplog.records)
        # Before the fix the refusal's terminal frame retired the marker by session_key: the live turn's.
        assert read_turn_marker(env.home, KEY) is not None, "the refused submit deleted the live turn's marker"
        # The refused submit's own state is still released as before: its claim and its card.
        assert sibling["running"] is False and "_notif_turn_claim" not in sibling
        errors = [p for e, sid, p in env.frames if e == "message.complete" and sid == "ui-sibling"]
        assert len(errors) == 1 and errors[0]["error_surface"]["code"] == "SESSION_NOT_OWNED"
        assert live["running"] is True
    finally:
        _finish(live, agent)
    # The marker's owner still retires it when its turn concludes.
    assert read_turn_marker(env.home, KEY) is None
    assert af.state(env.home)["reservations"] == 0


# --- T2: the retire backstop never deletes a live sibling process's marker ----------------------------------------

def _write_marker(home, writer_pid):
    path = home / "desktop" / "interrupted_turns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({KEY: {"attempts": 0, "prompt": "p", "started_at": time.time(),
                                      "auto_continue": True, "writer_pid": writer_pid}}))


@pytest.fixture()
def other_pids():
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    yield types.SimpleNamespace(live=live.pid, dead=dead.pid)
    live.kill()
    live.wait()


def test_retire_backstop_keeps_a_live_writers_marker_and_clears_a_dead_ones(env, other_pids, caplog):
    session = _record(env.home, types.SimpleNamespace(), "ui-a")
    caplog.set_level(logging.INFO)
    _write_marker(env.home, other_pids.live)  # process B, alive, mid-turn on the shared HERMES_HOME
    server._retire_turn_marker(session)
    assert read_turn_marker(env.home, KEY) is not None, "process A deleted live process B's marker"
    assert any("turn marker for session" in r.getMessage() and KEY in r.getMessage()
               and str(other_pids.live) in r.getMessage() for r in caplog.records)

    _write_marker(env.home, other_pids.dead)  # a dead writer's marker is still cleared
    server._retire_turn_marker(session)
    assert read_turn_marker(env.home, KEY) is None

    record_turn_start(env.home, KEY, "my own turn")  # this process's own marker is cleared
    server._retire_turn_marker(session)
    assert read_turn_marker(env.home, KEY) is None


def test_refusal_in_this_process_keeps_a_live_sibling_process_marker(env, other_pids):
    _write_marker(env.home, other_pids.live)
    _refuse_sibling_notice(env)
    assert read_turn_marker(env.home, KEY)["writer_pid"] == other_pids.live


# --- T4: end to end, the clarify is withdrawn and the reservation released ---------------------------------------

def test_clarify_blocked_turn_still_pauses_after_a_refused_sibling_submit(env, monkeypatch):
    surface_entered, surface_answer = threading.Event(), threading.Event()
    seen: dict = {}

    class Surface:
        cancel_reasons: list = []

        def __call__(self, questions):
            surface_entered.set()
            surface_answer.wait(WAIT_S)
            return {"answers": {}, "outcome": "cancelled"}

        def cancel_early(self, reason):
            self.cancel_reasons.append(reason)
            surface_answer.set()
            return True

    def body(agent):
        seen["clarify"] = json.loads(clarify_tool(QUESTIONS, callback=Surface(), session_id=KEY, tool_call_id="tc1"))
        seen["boundary"] = agent.step_boundary_pause() if agent.step_boundary_pause else None
        # The agent loop ends a turn its boundary hook paused like this (run_agent's paused_for_update).
        return {"final_response": "", "messages": [], "paused_for_update": seen["boundary"]}

    live, agent = _start_live_turn(env, monkeypatch, body)
    try:
        assert surface_entered.wait(WAIT_S), "the live turn never blocked in its clarify"
        _refuse_sibling_notice(env)
        af.set_hold(env.home)
        af.request_pause(env.home, expires_at=time.time() + 120)
    finally:
        _finish(live, agent)
    assert seen["clarify"]["status"] == up.CLARIFY_PAUSE_STATUS, seen
    assert seen["boundary"] == up.PAUSE_REASON
    assert read_turn_marker(env.home, KEY)["reason"] == up.PAUSE_REASON
    assert live["running"] is False and "_turn_admission" not in live
    assert af.state(env.home)["reservations"] == 0, "the live turn's reservation was not released"


# --- T5: a refused submit on the live turn's own record leaves its running state alone (defensive) ----------------
# Every current caller claims ``running`` before it submits, so none reaches this today; the record state is set up
# by a real admitted turn and the submit functions are called directly on it.

def test_refused_submit_on_the_live_record_keeps_running_ref_and_inflight(env, monkeypatch):
    live, agent = _start_live_turn(env, monkeypatch)
    try:
        with live["history_lock"]:
            token = live["_turn_admission"]
            refs, inflight, claim = token.refs, live["inflight_turn"], live["_notif_turn_claim"]
        env.slot["refuse"] = NOT_OWNED
        assert server._notif_submit("rid-n", "ui-live", live, "kanban notice", "kanban dispatch failed") is False
        assert server._run_prompt_submit("rid-p", "ui-live", live, "another submit") is False
        with live["history_lock"]:
            assert live["running"] is True
            assert live["_turn_admission"] is token and token.refs == refs
            assert live["inflight_turn"] is inflight and live["inflight_turn"].get("status") != "error"
            assert live["_notif_turn_claim"] is claim
        assert read_turn_marker(env.home, KEY) is not None
        assert not [p for e, sid, p in env.frames if e == "message.complete"], "the live turn's client got a refusal"
        assert server._notif_claim_turn(live) is False, "a second submit started over the live turn"
    finally:
        _finish(live, agent)
    assert live["running"] is False
    assert af.state(env.home)["reservations"] == 0


# --- T7: a live turn that lost its marker is a WARNING, once -------------------------------------------------------

def test_pause_hook_warns_once_when_a_live_turn_lost_its_marker(env, monkeypatch, caplog):
    agent = types.SimpleNamespace(session_id=KEY, step_boundary_pause=lambda: None)
    session = _record(env.home, agent, "ui-warn")
    session.update(running=True, _active_turn_marker_key=KEY)
    monkeypatch.setitem(server._sessions, "ui-warn", session)
    af.set_hold(env.home)
    af.request_pause(env.home, expires_at=time.time() + 120)
    caplog.set_level(logging.INFO)
    assert up.clarify_pause_provider(KEY, "tc1") is None
    assert up.clarify_pause_provider(KEY, "tc1") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and "live turn lost its marker" in r.getMessage()]
    assert len(warnings) == 1 and KEY in warnings[0].getMessage()

    caplog.clear()
    session["_active_turn_marker_key"] = "another-key"  # not this turn's marker: the ordinary decline only
    session.pop("_lost_marker_warned", None)
    assert up.not_resumable_reason(session, KEY) == "no turn marker"
    assert not [r for r in caplog.records if "live turn lost its marker" in r.getMessage()]


# --- review: the live-turn record means "running" ---------------------------------------------------------------

def _refused_direct_claim(env, session, sid):
    """A caller that claims ``running`` without ``admission()`` (the kickoff's way), then is refused."""
    with session["history_lock"]:
        session["running"] = True
    env.slot["refuse"] = NOT_OWNED
    assert server._run_prompt_submit("rid", sid, session, "hello") is False


def test_settle_prompt_state_drops_the_record_and_a_refusal_after_it_is_normal(env):
    from tui_gateway import turn_admission
    session = _record(env.home, types.SimpleNamespace(session_id=KEY, clear_interrupt=lambda: None), "ui-settle")
    with session["history_lock"]:
        session["running"] = True
        turn_admission.mark_live_turn(session)
    # A turn whose failure was retained (error_retained) ends through settle_prompt_state alone.
    st = types.SimpleNamespace(agent=types.SimpleNamespace(), error_retained=True)
    turn_admission.settle_prompt_state(session, st)
    assert turn_admission.LIVE_TURN not in session and session["running"] is False
    _refused_direct_claim(env, session, "ui-settle")
    assert session["running"] is False, "a refusal after the turn ended was treated as a live turn's sibling"
    assert [p for e, sid, p in env.frames if e == "message.complete" and sid == "ui-settle"]


def test_settle_drops_the_record_of_a_turn_that_never_started(env, monkeypatch):
    from tui_gateway.turn_admission import LIVE_TURN
    session = _record(env.home, types.SimpleNamespace(session_id=KEY, clear_interrupt=lambda: None), "ui-x")
    session["running"] = True
    # Admitted, then not started (another record is registered under this sid): settle runs, settle_prompt_state not.
    monkeypatch.setitem(server._sessions, "ui-x", _record(env.home, None, "ui-x-other"))
    assert server._run_prompt_submit("rid", "ui-x", session, "hello") is False
    assert LIVE_TURN not in session and session["running"] is False
    _refused_direct_claim(env, session, "ui-x")
    assert session["running"] is False


def test_missing_agent_refusal_never_marks_a_live_turn_nor_retires_a_sibling_marker(env, monkeypatch):
    from tui_gateway.turn_admission import LIVE_TURN
    record_turn_start(env.home, KEY, "a sibling record's live turn")
    session = _record(env.home, None, "ui-noagent")
    session["running"] = True
    seen: list = []
    real = server._emit_terminal_turn_error

    def spy(*args, **kwargs):  # before _fail_inflight_turn's settle can drop a record set too early
        seen.append(LIVE_TURN in session)
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_emit_terminal_turn_error", spy)
    assert server._admit_prompt_turn("ui-noagent", session, "hello", None, None, None, None) is None
    assert seen == [False], "the agent-None branch marked a live turn"
    assert read_turn_marker(env.home, KEY)["prompt"] == "a sibling record's live turn"


def test_agent_init_failure_keeps_a_sibling_marker(env, monkeypatch):
    record_turn_start(env.home, KEY, "a sibling record's live turn")
    session = _record(env.home, None, "ui-init")
    session["running"] = True
    monkeypatch.setattr(server, "_wait_agent_for_prompt",
                        lambda *a, **k: {"error": {"message": "agent initialization failed"}})
    server._run_after_agent_ready("rid", "ui-init", session, "hello", None, None, None)
    assert [p for e, sid, p in env.frames if e == "message.complete" and sid == "ui-init"]
    assert read_turn_marker(env.home, KEY)["prompt"] == "a sibling record's live turn"


def test_kickoff_releases_its_claim_only_when_no_live_turn_holds_the_record(env, monkeypatch, caplog):
    from tui_gateway import turn_admission
    from tui_gateway.turn_marker import mark_turn_paused

    class Inline:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
            self._t, self._a, self._k = target, args, kwargs or {}

        def start(self):
            self._t(*self._a, **self._k)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(server.threading, "Thread", Inline)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    caplog.set_level(logging.INFO)
    results = {}
    for live in (False, True):
        record_turn_start(env.home, KEY, "fix the flaky test", attempts=1)
        mark_turn_paused(env.home, KEY, up.PAUSE_REASON)
        session = _record(env.home, LiveAgent(), f"ui-k{live}")
        calls = []

        def slot(sid, s, _calls=calls, _live=live, **kw):
            _calls.append(sid)
            if len(_calls) == 1:  # the kickoff's own check passes; a live turn takes the record (defensive case)
                if _live:
                    turn_admission.mark_live_turn(s)
                return None
            return NOT_OWNED  # admission's check refuses

        monkeypatch.setattr(server, "_ensure_active_session_slot", slot)
        assert server._maybe_schedule_auto_continue(f"ui-k{live}", session, KEY) is not None
        results[live] = (session["running"], "_notif_turn_claim" in session, session.get("_auto_continue_scheduled"))
    assert results[False] == (False, False, False), "a refused kickoff kept its own claim"
    assert results[True][:2] == (True, True), "a refused kickoff released a live turn's running/claim"
    assert results[True][2] is False
    assert sum("never started" in r.getMessage() for r in caplog.records) == 2


def test_a_stale_spared_verdict_never_leaks_into_a_later_submit(env, monkeypatch):
    from tui_gateway.turn_admission import note_refusal
    session = _record(env.home, types.SimpleNamespace(session_id=KEY, clear_interrupt=lambda: None), "ui-stale")
    assert server._notif_claim_turn(session) is True
    note_refusal(spared_live_turn=True)  # an earlier refusal on this thread spared a live turn
    # This submit is refused before admission ever runs (so admission's own reset never happens).
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: False)
    assert server._notif_submit("rid", "ui-stale", session, "notice", "dispatch failed") is False
    assert session["running"] is False and "_notif_turn_claim" not in session, "the stale verdict kept the claim"


def test_a_fresh_claim_drops_a_leftover_live_turn_record_and_its_refusal_releases(env):
    from tui_gateway.turn_admission import LIVE_TURN
    session = _record(env.home, types.SimpleNamespace(session_id=KEY, clear_interrupt=lambda: None), "ui-claim")
    session[LIVE_TURN] = object()  # a record no turn settled (a gap the review traced): never a live turn here
    assert server._notif_claim_turn(session) is True
    env.slot["refuse"] = NOT_OWNED
    assert server._notif_submit("rid", "ui-claim", session, "notice", "dispatch failed") is False
    assert session["running"] is False and "_turn_admission" not in session
    assert af.state(env.home)["reservations"] == 0


# --- review: the lost-marker WARNING skips a turn retiring its own marker --------------------------------------------

def test_no_lost_marker_warning_while_a_turn_concludes_but_a_lost_marker_still_warns(env, monkeypatch, caplog):
    session = _record(env.home, types.SimpleNamespace(), "ui-c")
    session.update(running=True, _active_turn_marker_key=KEY, _turn_concluding_marker_key=KEY)
    caplog.set_level(logging.INFO)
    assert up.not_resumable_reason(session, KEY) == "no turn marker"
    assert not [r for r in caplog.records if "live turn lost its marker" in r.getMessage()]
    session.pop("_turn_concluding_marker_key")
    assert up.not_resumable_reason(session, KEY) == "no turn marker"
    assert [r for r in caplog.records if "live turn lost its marker" in r.getMessage()]


def test_a_pause_poll_during_post_turn_work_does_not_warn(env, monkeypatch, caplog):
    polled: list = []

    def after_complete(sid, session, st, raw):  # post-turn work: the marker is already retired, running still True
        polled.append((session.get("running"), up.not_resumable_reason(session, KEY)))

    monkeypatch.setattr(server, "_after_complete_turn", after_complete)
    caplog.set_level(logging.INFO)
    live, agent = _start_live_turn(env, monkeypatch)
    _finish(live, agent)
    assert polled == [(True, "no turn marker")], polled
    assert not [r for r in caplog.records if "live turn lost its marker" in r.getMessage()]


def _lost_marker_warnings(caplog):
    return [r for r in caplog.records if "live turn lost its marker" in r.getMessage()]


def test_the_next_turn_warns_when_a_sibling_deletes_its_marker(env, monkeypatch, caplog):
    from tui_gateway.turn_marker import clear_turn_marker
    live, agent = _start_live_turn(env, monkeypatch)  # turn A
    _finish(live, agent)  # A concluded: it retired its own marker (the concluding stamp)
    caplog.set_level(logging.INFO)
    agent_b = LiveAgent()
    live["agent"] = agent_b
    with live["history_lock"]:
        live["running"] = True
    assert server._run_prompt_submit("rid-b", "ui-live", live, "turn B") is True
    assert agent_b.entered.wait(WAIT_S)
    try:
        assert read_turn_marker(env.home, KEY)["prompt"] == "turn B"
        clear_turn_marker(env.home, KEY)  # a sibling deletes turn B's live marker
        assert up.not_resumable_reason(live, KEY) == "no turn marker"
        assert _lost_marker_warnings(caplog), "turn A's concluding stamp hid turn B's lost marker"
    finally:
        _finish(live, agent_b)


def test_a_kept_marker_deleted_during_post_turn_work_still_warns(env, monkeypatch, caplog):
    from tui_gateway.turn_marker import clear_turn_marker
    polled: list = []

    def after_complete(sid, session, st, raw):  # the turn kept its marker (no retire ran); a sibling deletes it
        clear_turn_marker(env.home, KEY)
        polled.append(up.not_resumable_reason(session, KEY))

    def body(agent):
        agent.release.wait(WAIT_S)
        # A held refused turn's marker: _retire_turn_marker returns early and keeps it.
        server._sessions["ui-live"]["_refused_turn_marker_key"] = KEY
        return {"final_response": "done", "messages": []}

    monkeypatch.setattr(server, "_after_complete_turn", after_complete)
    monkeypatch.setattr(server, "_schedule_refused_turn_retry", lambda *a, **k: None)
    caplog.set_level(logging.INFO)
    live, agent = _start_live_turn(env, monkeypatch, body)
    _finish(live, agent)
    assert polled == ["no turn marker"], polled
    assert _lost_marker_warnings(caplog), "a kept marker deleted by a sibling did not warn"
