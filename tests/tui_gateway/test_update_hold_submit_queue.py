"""An idle prompt.submit refused only by the core-update admission hold is queued, never bounced.

The update drainer holds admission for a bounded drain window while in-flight turns finish (hermes-sync
``drain_window``, t_01a181e61e88e5d4). Desktop has no handling for 5035, so a refusal there showed the
user an error for the message they had just sent. The message now joins the durable queue (persisted at
accept) and the admission retry runs it once the hold lifts. A retiring backend keeps its 5035.
"""
import threading
import types

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tui_gateway import admission_retry, server

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def held(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    timers = []

    class Timer:
        def __init__(self, delay, target):
            self.target = target
            self.daemon = True

        def start(self):
            timers.append(self)

    monkeypatch.setattr(admission_retry.threading, "Timer", Timer)
    persisted = []
    monkeypatch.setattr(server, "_persist_queued_user_row",
                        lambda session, envelope, kind: persisted.append((envelope["text"], kind)))
    dispatched = []

    def dispatch(rid, sid, record, envelope, generation):
        dispatched.append(envelope["text"])
        record["running"] = False
        server._clear_inflight_turn(record)
        return True

    monkeypatch.setattr(server, "_dispatch_claimed_queue", dispatch)
    session = {
        "agent": types.SimpleNamespace(valid_tool_names=set()),
        "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
        "history_version": 0, "running": False, "transport": None, "attached_images": [],
        "profile_home": str(tmp_path),
    }
    server._sessions["sid"] = session
    af.set_hold(tmp_path)
    yield types.SimpleNamespace(home=tmp_path, fence=fence, timers=timers, persisted=persisted,
                                dispatched=dispatched, session=session)
    server._sessions.pop("sid", None)
    af.clear_hold(tmp_path)


def _submit(**params):
    return server._methods["prompt.submit"]("r1", {"session_id": "sid", "text": "hello there", **params})


def test_idle_submit_under_the_update_hold_is_queued_and_runs_after_it(held):
    resp = _submit()
    assert "error" not in resp, resp
    assert resp["result"]["status"] == "queued"
    assert held.session["queued_prompt"]["text"] == "hello there"
    assert held.session["running"] is False
    assert held.persisted == [("hello there", None)], "durable at accept: an update restart cannot lose it"
    assert len(held.timers) == 1
    assert af.state(held.home)["reservations"] == 0, "queued, not admitted"
    # Still held: the retry re-arms without dispatching.
    held.timers.pop().target()
    assert not held.dispatched and len(held.timers) == 1
    af.clear_hold(held.home)
    held.timers.pop().target()
    assert held.dispatched == ["hello there"]
    assert not held.session.get("queued_prompt")


def test_second_send_under_the_hold_merges_into_the_same_queue(held):
    _submit()
    resp = _submit(text="and another")
    assert resp["result"]["status"] == "queued"
    assert held.session["queued_prompt"]["text"] == "hello there\n\nand another"
    assert len(held.timers) == 1, "one retry timer per session"


def test_retiring_backend_keeps_its_5035(held):
    token = held.fence.prepare()["token"]
    try:
        resp = _submit()
    finally:
        held.fence.cancel(token)
    assert resp["error"]["code"] == 5035
    assert not held.session.get("queued_prompt")
    assert not held.timers


def test_a_rewind_under_the_hold_is_refused_not_queued(held):
    resp = _submit(truncate_before_user_ordinal=0, confirm_truncate=True)
    assert resp["error"]["code"] == 5035
    assert not held.session.get("queued_prompt")


def test_without_a_hold_the_submit_is_admitted_normally(held, monkeypatch):
    af.clear_hold(held.home)
    ran = []
    monkeypatch.setattr(server, "_dispatch_admitted_prompt", lambda rid, *a: ran.append(a) or server._ok(rid, {"status": "streaming"}))
    resp = _submit()
    assert resp["result"]["status"] == "streaming"
    assert ran and not held.session.get("queued_prompt")
