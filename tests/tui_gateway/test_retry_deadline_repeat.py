"""A repeated update must reach an already-running restored retry wait."""
import threading
import time

from agent import provider_retry_deadline as prd
from hermes_cli import admission_fence as af
from tui_gateway import server, turn_marker
from tests.tui_gateway import test_retry_deadline_gateway as gw

rig = gw.rig


def test_repeat_pause_enters_restored_wait_before_request(rig, monkeypatch):
    home, make = rig
    provider = gw._Provider()
    agent = make(provider)
    deadline = gw._first_turn_paused(home, agent, provider)
    entered = threading.Event()
    real_sleep = prd._sleep

    def sleep(seconds):
        entered.set()
        real_sleep(seconds)

    monkeypatch.setattr(prd, "_sleep", sleep)
    fresh = gw._session(agent, home)
    fresh["running"] = False
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)

    def issue():
        if entered.wait(10):
            af.set_hold(home)
            af.request_pause(home, expires_at=time.time() + 120)

    issuer = threading.Thread(target=issue)
    issuer.start()
    try:
        assert server._maybe_schedule_auto_continue("repeat-sid", fresh, gw.KEY) is not None
        gw._join_scheduler()
        gw._join_turn(fresh)
    finally:
        issuer.join(15)
    assert entered.is_set()
    marker = turn_marker.read_turn_marker(home, gw.KEY)
    assert marker and marker.get("provider_retry_not_before") == deadline, marker
    assert marker["reason"] == "core_update_pause" and marker["pauses"] == 2
    assert len(provider.times) == 2
    assert time.time() < deadline, "must yield before original retry epoch, not after serving it"
