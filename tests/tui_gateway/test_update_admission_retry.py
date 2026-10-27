"""Queued prompts remain queued and retry without admitting under a hold."""
import threading

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tui_gateway import admission_retry, server

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("hold", [False, True])
def test_queue_retries_after_admission_unavailable(tmp_path, monkeypatch, hold):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    timers = []
    class Timer:
        def __init__(self, delay, target):
            self.target = target
        def start(self):
            timers.append(self)
    monkeypatch.setattr(admission_retry.threading, "Timer", Timer)
    queued = {"text": "keep me"}
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(),
               "running": False, "queued_prompt": queued}
    dispatched = []
    def dispatch(rid, sid, record, envelope, generation):
        dispatched.append(envelope)
        record["running"] = False
        server._clear_inflight_turn(record)
        return True
    monkeypatch.setattr(server, "_dispatch_claimed_queue", dispatch)
    if hold:
        af.set_hold(tmp_path)
        assert server._drain_queued_prompt("r", "s", session)
        assert server._drain_queued_prompt("r", "s", session)
    else:
        with af.admission_lock(tmp_path):
            assert server._drain_queued_prompt("r", "s", session)
            assert server._drain_queued_prompt("r", "s", session)
    assert len(timers) == 1
    assert session["queued_prompt"] is queued
    assert not session["running"]
    assert af.state(tmp_path)["reservations"] == 0
    if hold:
        timers.pop().target()
        assert len(timers) == 1
        assert not dispatched
        af.clear_hold(tmp_path)
    timers.pop().target()
    assert dispatched == [queued]
    assert not session.get("queued_prompt")
    assert af.state(tmp_path)["reservations"] == 0
    assert not session.get("_admission_retry")


def test_pending_retry_does_not_resurrect_closed_queue(monkeypatch):
    timers = []
    class Timer:
        def __init__(self, delay, target):
            self.target = target
        def start(self):
            timers.append(self)
    monkeypatch.setattr(admission_retry.threading, "Timer", Timer)
    session = {"history_lock": threading.RLock(), "queued_prompt": {"text": "x"}}
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: pytest.fail("closed session retried"))
    with session["history_lock"]:
        assert admission_retry.retry_queued_prompt("r", "s", session)
        session["_closing"] = True
    timers[0].target()
    assert "_admission_retry" not in session
