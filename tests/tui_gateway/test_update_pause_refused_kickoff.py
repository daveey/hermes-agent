"""[daveey] A continuation whose submit is refused during its own start-up keeps an owner.

Hub incident (defect gateway-833d39a686): a core-update pause window ended, the in-place watcher scheduled the
paused turn's continuation and exited. The kickoff waited ~18 s for the agent build; meanwhile the drain re-took
the update hold for another chat, so ``_run_prompt_submit`` refused the turn at admission (returned False). The
kickoff ignored that result: ``_auto_continue_scheduled`` stayed True, no watcher was left, and the chat sat
paused for 40 minutes until the backend exited. "Scheduled" was treated as "started".

Contract pinned here: a refused kickoff releases its claim and logs that the continuation never started; a
paused continuation re-arms the in-place watcher, which continues the turn exactly once after the hold lifts,
with no attempt used; a crash continuation backs off instead of hanging.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import clear_turn_marker, mark_turn_paused, read_turn_marker, record_turn_start

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


def _session(**extra):
    return {"agent": types.SimpleNamespace(), "session_key": "session-key", "history": [],
            "history_lock": threading.RLock(), "history_version": 0, "running": False, "attached_images": [],
            "image_counter": 0, "cols": 80, "slash_worker": None, "show_reasoning": False,
            "tool_progress_mode": "all", "inflight_turn": None, **extra}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    hold = {"retaken": True}  # the drain re-took the hold while the kickoff waited for its build
    ran: list = []

    def submit(rid, sid, session, text, **kw):
        if hold["retaken"]:
            return False  # prompt_turn: prepare_work refused the reservation (can_start False)
        ran.append((text, session.get("_auto_continue_attempt")))
        with session["history_lock"]:
            session["running"] = False
            session.pop("_auto_continue_scheduled", None)
        clear_turn_marker(tmp_path, "session-key")  # the turn ran and concluded
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    watchers: list = []
    real_watcher = up.start_in_place_watcher
    monkeypatch.setattr(up, "start_in_place_watcher", lambda sid, session, key: watchers.append((sid, key)))
    return types.SimpleNamespace(home=tmp_path, hold=hold, ran=ran, watchers=watchers, real_watcher=real_watcher)


def test_continuation_refused_during_its_build_runs_once_after_the_hold_lifts(env):
    record_turn_start(env.home, "session-key", "fix the flaky test", attempts=1)
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session()

    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is not None  # "scheduled"
    assert not env.ran
    assert env.watchers == [("sid", "session-key")], \
        "the refused continuation was dropped: the paused turn never ran"

    env.hold["retaken"] = False  # the hold lifts; the re-armed watcher schedules again after the short backoff
    assert time.monotonic() < session["_auto_continue_retry_after"] <= time.monotonic() + 10.0
    session.pop("_auto_continue_retry_after")
    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is not None
    assert env.ran == [(up.pause_note("fix the flaky test"), 1)]  # exactly once, no attempt used
    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is None
    assert len(env.ran) == 1


def test_refused_kickoff_clears_its_claim_and_rearms_a_watcher(env):
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session()

    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert not session.get("_auto_continue_scheduled")
    assert not session.get("running")
    assert env.watchers == [("sid", "session-key")]
    assert read_turn_marker(env.home, "session-key") is not None  # the marker is still the turn's record


def test_refused_crash_continuation_backs_off_instead_of_hanging(env):
    record_turn_start(env.home, "session-key", "the prompt")
    session = _session()

    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert not (session.get("_auto_continue_scheduled") or session.get("running"))
    assert session.get("_auto_continue_retry_after", 0) > time.monotonic()
    assert env.watchers == []  # not a pause: the backoff, not a watcher, owns the retry
    assert read_turn_marker(env.home, "session-key") is not None


def test_refusal_warning_names_who_retries(env, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    server._maybe_schedule_auto_continue("sid", _session(), "session-key")
    clear_turn_marker(env.home, "session-key")
    record_turn_start(env.home, "session-key", "the prompt")  # a crash marker: not a pause
    server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    warnings = [r.getMessage() for r in caplog.records if "never started" in r.getMessage()]
    assert len(warnings) == 2
    assert warnings[0].endswith(f"rescheduling through its pause watcher (re-arm 1/{server.REFUSED_REARM_MAX})")
    # Nothing re-polls a crash continuation: the warning names who does retry, not a timer that does not exist.
    assert warnings[1].endswith(
        "not rescheduled here (30s backoff): the next resume, boot sweep or refused-turn timer retries")
    assert "rescheduling after" not in warnings[1]


@pytest.mark.parametrize("flag", ["_closing", "_finalized"])
def test_paused_continuation_refused_on_a_closing_record_does_not_rearm(env, monkeypatch, flag):
    """A record that closed during the build gets no watcher: it backs off like any other refusal."""
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session()

    def submit(rid, sid, s, text, **kw):
        s[flag] = True  # the record closed while the kickoff waited for its build
        return False

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert env.watchers == []
    assert not session.get("_auto_continue_scheduled")
    assert session["_auto_continue_retry_after"] > time.monotonic() + 20.0  # the 30 s backoff, not 10 s


@pytest.mark.parametrize("result", ["none", "raises"])
def test_submit_that_is_not_a_refusal_never_rearms(env, monkeypatch, result):
    """Only an explicit False is a refusal: a None-returning submit is not one, and a submit that raised was
    already handled as a dispatch failure (its claim released) and must not also be rescheduled."""
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session()

    def raising(rid, sid, s, text, **kw):
        raise RuntimeError("boom")

    submit = raising if result == "raises" else (lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert env.watchers == []
    assert "_auto_continue_retry_after" not in session


def test_accepted_continuation_is_unchanged(env):
    env.hold["retaken"] = False
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session()

    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert len(env.ran) == 1 and env.watchers == []
    assert "_auto_continue_retry_after" not in session


def test_watcher_whose_continuation_is_refused_hands_off_to_a_rearmed_watcher(env, monkeypatch, caplog):
    """The real watch loop end to end: the window is over, the watcher schedules and exits, the kickoff's submit
    is refused (the hold re-taken during its build). The refusal is logged and re-arms one watcher, which
    continues the turn exactly once after the hold lifts, with no attempt used."""
    import logging
    record_turn_start(env.home, "session-key", "fix the flaky test", attempts=1)
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session(profile_home=str(env.home))
    clock, rearmed = [0.0], []

    def submit(rid, sid, s, text, **kw):
        if env.hold["retaken"]:
            env.hold["retaken"] = False  # refused once; the hold lifts afterwards
            return False
        env.ran.append((text, s.get("_auto_continue_attempt")))
        clear_turn_marker(env.home, "session-key")  # the turn ran and concluded
        return True

    def rearm(sid, s, key):
        rearmed.append(key)
        s.pop("_auto_continue_retry_after", None)  # the short backoff elapses before its poll
        return env.real_watcher(sid, s, key)

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    monkeypatch.setattr(up, "start_in_place_watcher", rearm)
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    monkeypatch.setattr(up.threading, "Thread", _InlineThread)
    monkeypatch.setattr(up, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s), time=time.time))
    monkeypatch.setattr(up, "WATCH_MAX_S", 120.0)
    caplog.set_level(logging.WARNING)

    env.real_watcher("sid", session, "session-key")

    assert env.ran == [(up.pause_note("fix the flaky test"), 1)]
    assert rearmed == ["session-key"]
    assert any("continuation of session-key never started" in r.getMessage() for r in caplog.records)


_KICKOFF_KEYS = ("_auto_continue_attempt", "_auto_continue_prompt", "_auto_continue_pauses",
                 "_auto_continue_killed_jobs", "_marker_until_tool")


@pytest.mark.parametrize("result", ["refused", "raises"])
def test_kickoff_that_never_started_leaves_no_marker_inputs(env, monkeypatch, result):
    """The kickoff sets the marker inputs a started turn reads back (_record_turn_marker pops them). A submit that
    was refused, or a dispatch that raised, must drop them: the next turn to record (a user's) would otherwise
    write its crash marker with the paused prompt, its attempt count and the at-most-once flag."""
    record_turn_start(env.home, "session-key", "the paused prompt", attempts=1, until_tool=True)
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON, killed_jobs=[{"id": "job-1"}])
    session = _session()
    seen = {}

    def submit(rid, sid, s, text, **kw):
        seen.update({k: s.get(k) for k in _KICKOFF_KEYS})
        if result == "raises":
            raise RuntimeError("boom")
        return False

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert seen["_auto_continue_prompt"] == "the paused prompt" and seen["_marker_until_tool"] is True  # were set
    assert [k for k in _KICKOFF_KEYS if k in session] == []


def test_persistently_refused_paused_continuation_stops_rearming_at_the_cap(env, caplog):
    """Each re-arm starts a fresh watcher with a fresh budget, so a refusal that never clears would loop every
    ~10-15 s until the marker aged out. The re-arms are capped per pause; past the cap the refusal backs off like
    any other and the next resume, boot sweep or refused-turn timer retries."""
    import logging
    caplog.set_level(logging.WARNING)
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session()

    for _ in range(server.REFUSED_REARM_MAX + 3):
        session.pop("_auto_continue_retry_after", None)  # the backoff elapses before the next poll
        server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert len(env.watchers) == server.REFUSED_REARM_MAX
    assert session["_auto_continue_retry_after"] > time.monotonic() + 20.0  # the 30 s backoff past the cap
    assert not session.get("_auto_continue_scheduled")
    last = [r.getMessage() for r in caplog.records if "never started" in r.getMessage()][-1]
    assert f"re-arm cap {server.REFUSED_REARM_MAX} reached" in last and "not rescheduled here" in last
    assert read_turn_marker(env.home, "session-key") is not None

    # A later pause of the turn is a new pause: its continuation gets its own re-arms.
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session.pop("_auto_continue_retry_after", None)
    server._maybe_schedule_auto_continue("sid", session, "session-key")
    assert len(env.watchers) == server.REFUSED_REARM_MAX + 1


def test_paused_continuation_refused_for_ownership_does_not_rearm(env, monkeypatch, caplog):
    """An ownership refusal at _admit_prompt_turn (the chat moved to another gateway) does not lift when a hold
    does: no watcher waits it out, so the refusal backs off instead of re-arming."""
    import logging
    caplog.set_level(logging.WARNING)
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session()

    def submit(rid, sid, s, text, **kw):
        s["_turn_owner_refusal"] = "SESSION_MOVED"  # what _admit_prompt_turn records on its ownership fence
        return False

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert env.watchers == []
    assert session["_auto_continue_retry_after"] > time.monotonic() + 20.0
    assert any("owner refusal SESSION_MOVED" in r.getMessage() for r in caplog.records)


def test_stale_owner_refusal_from_an_earlier_submit_does_not_block_the_rearm(env, caplog):
    """``_turn_owner_refusal`` left by an earlier ownership-refused prompt.submit survives a refusal that returns
    before _admit_prompt_turn clears it (here: the hold re-taken at admission). The kickoff clears it before its
    own submit, so that refusal re-arms the watcher instead of being misread as an owner refusal."""
    import logging
    caplog.set_level(logging.WARNING)
    record_turn_start(env.home, "session-key", "the prompt")
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    session = _session(_turn_owner_refusal="SESSION_MOVED")  # stale: an earlier user submit was refused

    server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert env.watchers == [("sid", "session-key")]
    assert session["_auto_continue_retry_after"] < time.monotonic() + 15.0  # the short re-arm backoff
    assert not any("owner refusal" in r.getMessage() for r in caplog.records)
