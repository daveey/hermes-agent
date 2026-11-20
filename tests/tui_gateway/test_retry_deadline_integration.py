"""Durable provider-retry deadline: marker custody, stamp fencing, and real-turn pause/resume.

Real AIAgent retry loop, real flocked marker store, real admission fence and gateway hook/watcher. Only the provider
and the tool body are fakes. Clock-controlled cases drive the REAL ``compute_error_backoff`` (503 + Retry-After 600, no
credential pool) with a fake clock, so no provider call is observed before the original epoch; that is deterministic
evidence for the seam, NOT a live process proof (production policy is the real 600 s).
"""
from __future__ import annotations

import threading
import time

import httpx
import pytest
from openai import InternalServerError

from agent import provider_retry_deadline as prd
from agent import turn_api_error, turn_recovery, update_pause_cancel
from hermes_cli import admission_fence as af
from tui_gateway import server, turn_marker
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import PROVIDER_RETRY_FIELD as F
from tui_gateway.turn_marker import read_turn_marker, record_turn_start
from tests.tui_gateway import test_update_pause_model_request as helpers

home = helpers.home
_quiet = helpers._quiet
KEY = "session-key"


class Clock:
    """Fake wall clock: each sleep advances ``speed`` x the slept time and only yields briefly in real time."""

    def __init__(self, start=None, speed=5.0):
        self.t, self.speed = start or time.time(), speed

    def time(self):
        return self.t

    def sleep(self, s):
        time.sleep(0.002)  # health: allow HX013 -- simulated clock wait yields to the real cancellation watcher
        self.t += s * self.speed

    def __getattr__(self, name):
        return getattr(time, name)


def _own(home_):
    return read_turn_marker(home_, KEY)


def _session(home_):
    return {"session_key": KEY, "profile_home": str(home_), "history_lock": threading.Lock()}


def _hooks(agent, home_, restored=None, marker=None):
    stamp, clear = up.provider_retry_hooks(_session(home_), KEY, marker or _own(home_))
    prd.install(agent, stamp=stamp, clear=clear, restored=restored)


def _err503(retry_after="600"):
    req = httpx.Request("POST", "https://example.com/chat")
    return InternalServerError("overloaded", response=httpx.Response(503, request=req, headers={"retry-after": retry_after}),
                               body=None)


# ---- turn_marker seams ------------------------------------------------------------------------------------------
def test_marker_projection_replacement_and_first_write(tmp_path):
    assert record_turn_start(tmp_path, KEY, "p")
    gen = _own(tmp_path)
    d = time.time() + 600
    assert turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, gen)
    assert _own(tmp_path)[F] == d
    # first active write wins; a different one is refused, the same one is idempotent
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d + 50, gen)
    assert turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, gen)
    assert turn_marker.mark_turn_paused(tmp_path, KEY, "core_update_pause")
    assert _own(tmp_path)[F] == d  # pause keeps it, repeat pause too
    assert turn_marker.mark_turn_paused(tmp_path, KEY, "core_update_pause")
    assert _own(tmp_path)[F] == d
    # replacement: only an explicit continuation kwarg carries it
    assert record_turn_start(tmp_path, KEY, "p", pauses=2)
    assert F not in _own(tmp_path)
    assert record_turn_start(tmp_path, KEY, "p", pauses=2, provider_retry_not_before=d)
    assert _own(tmp_path)[F] == d
    for junk in (float("nan"), float("inf"), -1, True, "9"):
        assert record_turn_start(tmp_path, KEY, "p", provider_retry_not_before=junk) and F not in _own(tmp_path)


def test_expired_deadline_is_replaced_by_later_retry(tmp_path):
    assert record_turn_start(tmp_path, KEY, "p")
    gen = _own(tmp_path)
    old = time.time() - 5
    assert turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, old, gen)
    new = time.time() + 30
    assert turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, new, gen)
    assert _own(tmp_path)[F] == new
    assert turn_marker.clear_provider_retry_deadline(tmp_path, KEY, new, gen)
    assert F not in _own(tmp_path)


def test_stamp_fencing_refuses_missing_foreign_successor_and_empty(tmp_path, monkeypatch):
    d = time.time() + 60
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, {"started_at": 1.0})  # no marker
    assert record_turn_start(tmp_path, KEY, "p")
    gen = _own(tmp_path)
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, "", d, gen)
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, float("nan"), gen)
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, None)
    # foreign live writer
    monkeypatch.setattr(turn_marker, "_writer_identity", lambda: {"writer_pid": gen["writer_pid"] + 1})
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, gen)
    monkeypatch.undo()
    # successor turn (new generation) must not take the old turn's stamp, and is not mutated
    monkeypatch.setattr(turn_marker.time, "time", lambda: gen["started_at"] + 1)
    assert record_turn_start(tmp_path, KEY, "p2")
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, gen)
    assert F not in _own(tmp_path)
    # no marker (Stop retired it) after all
    turn_marker.clear_turn_marker(tmp_path, KEY)
    assert not turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, gen)


def test_carried_notice_gets_no_hooks_and_keeps_paused_deadline(tmp_path, monkeypatch):
    d = time.time() + 600
    assert record_turn_start(tmp_path, KEY, "orig")
    assert turn_marker.stamp_provider_retry_deadline(tmp_path, KEY, d, _own(tmp_path))
    assert turn_marker.mark_turn_paused(tmp_path, KEY, "core_update_pause")
    assert up.provider_retry_hooks(_session(tmp_path), "", _own(tmp_path)) == (None, None)
    assert up.provider_retry_hooks(_session(tmp_path), KEY, None) == (None, None)
    session = {**_session(tmp_path), "_synthesized_turn": True}
    monkeypatch.setattr(server, "_session_home", lambda _s: tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: helpers.CFG)
    assert server._record_turn_marker(session, "notice") == ""  # carried: no marker of its own
    assert _own(tmp_path)[F] == d and _own(tmp_path)["reason"] == "core_update_pause"


def test_fresh_user_turn_and_kickoff_input_cleanup(tmp_path, monkeypatch):
    from tui_gateway import session_auto_continue as sac
    assert "_auto_continue_provider_retry_not_before" in sac._KICKOFF_MARKER_INPUTS
    session = {**_session(tmp_path), "_auto_continue_provider_retry_not_before": 5.0e9}
    sac._drop_kickoff_marker_inputs(session, {})  # refused kickoff: inputs removed
    assert "_auto_continue_provider_retry_not_before" not in session
    monkeypatch.setattr(server, "_session_home", lambda _s: tmp_path)
    session = _session(tmp_path)  # a typed user message after it
    assert server._record_turn_marker(session, "hello") == KEY
    assert F not in _own(tmp_path)


# ---- agent seams ------------------------------------------------------------------------------------------------
def test_wait_restored_unit(tmp_path, monkeypatch):
    agent = helpers._make_agent(tmp_path)
    clock = Clock()
    monkeypatch.setattr(prd, "_now", clock.time)
    monkeypatch.setattr(prd, "_sleep", clock.sleep)
    cleared = []
    d = clock.t + 100
    prd.install(agent, stamp=None, clear=cleared.append, restored=d)
    assert prd.wait_restored(agent, None, [{"role": "user", "content": "x"}], 1) == "done"
    assert clock.t >= d and cleared == [d] and prd.RESTORED_ATTR not in vars(agent)
    assert prd.wait_restored(agent, None, [], 1) == "done"  # nothing left
    # expired deadline: no wait
    prd.install(agent, stamp=None, clear=None, restored=clock.t - 1)
    t0 = clock.t
    assert prd.wait_restored(agent, None, [], 1) == "done" and clock.t == t0
    # Stop during the restored wait raises into the provider path's Stop handling
    prd.install(agent, stamp=None, clear=None, restored=clock.t + 100)
    agent.interrupt()
    with pytest.raises(InterruptedError):
        prd.wait_restored(agent, None, [], 1)
    assert vars(agent)[prd.RESTORED_ATTR] > clock.t  # still owed
    prd.reset(agent)
    assert prd.RESTORED_ATTR not in vars(agent) and prd.STAMP_ATTR not in vars(agent)


def test_generic_waits_never_take_the_update_yield(tmp_path, monkeypatch):
    agent = helpers._make_agent(tmp_path)
    monkeypatch.setattr(update_pause_cancel, "model_yield_requested", lambda a: True)
    sentinel = {"interrupted": True}
    monkeypatch.setattr(turn_recovery, "abort_turn_on_interrupt", lambda *a, **k: sentinel)
    agent._interrupt_requested = True
    kw = dict(messages=[], conversation_history=None, api_call_count=1, abort_message="a", interrupt_text="i",
              activity_label="x")
    retry = type("R", (), {"restart_with_redirected_messages": False})()
    # default (empty-response / invalid-response / autorecovery sites): update cancel is NOT a yield
    assert turn_recovery.interruptible_backoff_sleep(agent, 5, retry, **kw) is sentinel
    assert retry.restart_with_redirected_messages is False
    # explicit opt-in yields
    assert turn_recovery.interruptible_backoff_sleep(agent, 5, retry, update_yield=True, **kw) is None
    assert retry.restart_with_redirected_messages is True


def _run_first_phase(home_, tmp_path, monkeypatch, *, stamp_mode="real", retry_after="600"):
    """Progressed turn; 503 Retry-After (REAL compute_error_backoff, no pool); update pause requested in the wait."""
    agent = helpers._make_agent(tmp_path)
    record_turn_start(home_, KEY, "run the quick job")
    _hooks(agent, home_)
    if stamp_mode == "refuse":
        agent.provider_retry_deadline_stamp = lambda d: False
    elif stamp_mode == "mismatch":
        real = turn_marker._update
        monkeypatch.setattr(turn_marker, "_update",
                            lambda h, k, m, what: True if what == "stamp retry deadline" else real(h, k, m, what))
    session = _session(home_)
    entered, issued, calls = threading.Event(), threading.Event(), []

    def provider(*a, **k):
        calls.append(time.time())
        if len(calls) == 1:
            return helpers._response(helpers._call("call_1"))
        if len(calls) == 2:
            raise _err503(retry_after)
        return helpers._response()

    real_sleep = turn_api_error.interruptible_backoff_sleep

    def observed(*a, **k):
        entered.set()
        assert issued.wait(5)
        return real_sleep(*a, **k)

    def pause():
        if entered.wait(10):
            af.set_hold(home_)
            af.request_pause(home_, expires_at=time.time() + 120)
            issued.set()

    agent._interruptible_api_call = provider
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: "quick result")
    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", observed)
    if stamp_mode != "real":  # keep the wait short: the point is that it runs to its ORIGINAL end without pausing
        monkeypatch.setattr(turn_api_error, "compute_error_backoff", lambda *a, **k: 2.0)
    reservation = af.reserve(home_)
    producer = threading.Thread(target=pause)
    producer.start()
    agent.step_boundary_pause = up.boundary_pause_hook(session, KEY)
    stop = up.start_mid_tool_cancel(session, KEY, agent)
    try:
        result = agent.run_conversation("run the quick job")
    finally:
        stop()
        producer.join(12)
        reservation.close()
    return agent, result, calls


@pytest.mark.parametrize("mode", ["refuse", "mismatch"])
def test_stamp_failure_and_readback_mismatch_keep_original_wait(home, tmp_path, monkeypatch, mode):
    _agent, result, calls = _run_first_phase(home, tmp_path, monkeypatch, stamp_mode=mode)
    assert not result.get("paused_for_update"), mode
    assert result["final_response"] == "done" and len(calls) == 3, mode
    assert calls[2] - calls[1] >= 1.9, mode  # ordinary original wait, to its end, then the provider
    assert F not in (read_turn_marker(home, KEY) or {}), mode


def _continue(home_, tmp_path, monkeypatch, marker, *, agent=None, clock=None):
    """Kickoff inputs -> _record_turn_marker -> the gateway's per-turn install -> run the continuation."""
    agent = agent or helpers._make_agent(tmp_path)
    session = {**_session(home_), "_auto_continue_prompt": marker["prompt"], "_auto_continue_attempt": 0,
               "_auto_continue_pauses": marker["pauses"],
               "_auto_continue_provider_retry_not_before": marker[F]}
    monkeypatch.setattr(server, "_session_home", lambda _s: home_)
    assert server._record_turn_marker(session, "Continue") == KEY
    assert "_auto_continue_provider_retry_not_before" not in session
    own = _own(home_)
    assert own[F] == marker[F]  # survived continuation replacement
    _hooks(agent, home_, restored=own[F], marker=own)
    return agent


@pytest.mark.parametrize("flow", ["cold", "in_place"])
def test_no_pool_503_retry_after_600_survives_pause_and_resume(home, tmp_path, monkeypatch, flow):
    agent1, result, calls = _run_first_phase(home, tmp_path, monkeypatch)
    assert result.get("paused_for_update") == up.PAUSE_REASON and len(calls) == 2
    marker = read_turn_marker(home, KEY)
    deadline = marker[F]
    assert marker["reason"] == up.PAUSE_REASON and 590 < deadline - time.time() <= 601
    assert getattr(agent1, "_credential_pool", None) is None
    assert af.state(home)["reservations"] == 0
    prd.reset(agent1)  # turn-end cleanup of the cached agent
    clock = Clock(start=time.time())
    for mod in (prd,):
        monkeypatch.setattr(mod, "_now", clock.time)
        monkeypatch.setattr(mod, "_sleep", clock.sleep)
    af.clear_hold(home) if hasattr(af, "clear_hold") else None
    agent2 = _continue(home, tmp_path, monkeypatch, marker, agent=agent1 if flow == "in_place" else None)
    seen = []
    agent2._interruptible_api_call = lambda *a, **k: seen.append(clock.t) or helpers._response()
    result2 = agent2.run_conversation("Continue")
    assert result2["final_response"] == "done"
    assert len(seen) == 1 and seen[0] >= deadline, (seen, deadline)  # not one call before the ORIGINAL epoch
    assert clock.t - deadline < 10  # waited for the original epoch, not a recomputed/extended one
    assert F not in (read_turn_marker(home, KEY) or {})  # served deadline cleared
    # next turn after terminal: nothing inherited
    prd.reset(agent2)
    assert prd.RESTORED_ATTR not in vars(agent2)


def test_repeat_pause_keeps_epoch_then_resume_waits_original(home, tmp_path, monkeypatch):
    _a, result, _calls = _run_first_phase(home, tmp_path, monkeypatch)
    assert result.get("paused_for_update")
    marker = read_turn_marker(home, KEY)
    deadline = marker[F]
    clock = Clock(start=time.time(), speed=40.0)
    monkeypatch.setattr(prd, "_now", clock.time)
    monkeypatch.setattr(prd, "_sleep", clock.sleep)
    # second pause DURING the restored wait (watcher cancels after the real grace, long before the epoch)
    agent2 = _continue(home, tmp_path, monkeypatch, marker)
    agent2.update_pause_first_request = True
    session = _session(home)
    af.set_hold(home)
    af.request_pause(home, expires_at=time.time() + 120)
    seen = []
    agent2._interruptible_api_call = lambda *a, **k: seen.append(clock.t) or helpers._response()
    agent2.step_boundary_pause = up.boundary_pause_hook(session, KEY)
    reservation = af.reserve(home) if hasattr(af, "reserve") else None
    stop = up.start_mid_tool_cancel(session, KEY, agent2)
    try:
        r2 = agent2.run_conversation("Continue")
    finally:
        stop()
        if reservation:
            reservation.close()
    assert r2.get("paused_for_update") == up.PAUSE_REASON and not seen
    marker2 = read_turn_marker(home, KEY)
    assert marker2[F] == deadline and marker2["pauses"] == marker["pauses"] + 1  # epoch neither shortened nor extended
    # third run: resume again; still no call before the same epoch
    af.clear_hold(home) if hasattr(af, "clear_hold") else None
    clock2 = Clock(start=clock.t, speed=40.0)
    monkeypatch.setattr(prd, "_now", clock2.time)
    monkeypatch.setattr(prd, "_sleep", clock2.sleep)
    agent3 = _continue(home, tmp_path, monkeypatch, marker2)
    agent3._interruptible_api_call = lambda *a, **k: seen.append(clock2.t) or helpers._response()
    assert agent3.run_conversation("Continue")["final_response"] == "done"
    assert seen and seen[0] >= deadline


def test_stop_during_restored_wait_makes_no_provider_call(home, tmp_path, monkeypatch):
    assert record_turn_start(home, KEY, "run the quick job")
    agent = helpers._make_agent(tmp_path)
    _hooks(agent, home, restored=time.time() + 600)
    calls = []
    agent._interruptible_api_call = lambda *a, **k: calls.append(1) or helpers._response()
    threading.Timer(0.6, agent.interrupt).start()
    result = agent.run_conversation("run the quick job")
    assert not calls and result.get("interrupted") is True and not result.get("paused_for_update")
    assert (read_turn_marker(home, KEY) or {}).get("reason") != up.PAUSE_REASON


def test_other_session_and_scheduler_are_not_blocked_by_a_future_deadline(home, tmp_path):
    from tui_gateway import startup_resume  # the sweep module must import without waiting
    assert record_turn_start(home, KEY, "orig")
    assert turn_marker.stamp_provider_retry_deadline(home, KEY, time.time() + 600, _own(home))
    assert turn_marker.mark_turn_paused(home, KEY, up.PAUSE_REASON)
    other = helpers._make_agent(tmp_path)  # another session's agent: no restored epoch, no wait
    other._interruptible_api_call = lambda *a, **k: helpers._response()
    t0 = time.time()
    assert other.run_conversation("hi")["final_response"] == "done"
    assert time.time() - t0 < 5
