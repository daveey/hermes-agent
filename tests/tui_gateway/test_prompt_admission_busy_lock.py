"""prompt.admit while a hub Kanban claim holds admission.lock for its ~1 s sample (review F1).

prompt.admit reserves under the session resume lock, so it never waits there (one short try). A briefly busy lock
must still not refuse the accepted decision: it waits OUTSIDE the resume lock, as a registered waiting turn, then
retries the whole admission once. Nothing durable happens before admission, so the retry cannot duplicate a turn.
"""
import threading
import time

import pytest

from hermes_cli import admission_fence as af
from tests.tui_gateway.test_prompt_admission import call, owner
from tests.waiting import wait_until
from tui_gateway import server

pytestmark = pytest.mark.platforms("posix")


def _root():
    from hermes_constants import get_default_hermes_root, get_hermes_home
    return get_default_hermes_root(home=get_hermes_home())


def _hold_lock(release, m):
    with af.admission_lock(_root(), timeout=1):
        m["held"].set()
        release.wait(5)
        m["released"] = time.monotonic()


def test_busy_update_lock_admits_once_after_the_hold_without_holding_the_resume_lock(owner):
    db, _session, params, started = owner
    release, m = threading.Event(), {"held": threading.Event()}
    holder = threading.Thread(target=_hold_lock, args=(release, m))
    holder.start()
    m["held"].wait(5)
    result = {}
    caller = threading.Thread(target=lambda: result.setdefault("r", call("prompt.admit", params)))
    caller.start()
    try:
        # While admit waits, it is a registered waiting turn and the resume lock stays free for other sessions.
        wait_until(lambda: af.turns_waiting(_root() / "core-update"), timeout=5, what="prompt.admit waiting")
        assert server._session_resume_lock.acquire(timeout=1)
        server._session_resume_lock.release()
        time.sleep(0.6)  # health: allow HX013 -- past the old one-try refusal point while the hold continues
        assert caller.is_alive()
    finally:
        release.set()
        holder.join(5)
        caller.join(10)
    response = result["r"]
    assert "result" in response, response
    assert response["result"]["status"] == "accepted"
    assert len(started) == 1  # dispatched exactly once
    assert len(db.get_messages(params["stored_session_id"])) == 1
    assert not af.turns_waiting(_root() / "core-update")
    # A lost reply's retry reads the same receipt back and never dispatches again.
    assert call("prompt.admit", params)["result"] == response["result"]
    assert len(started) == 1


def test_lock_busy_past_the_budget_still_refuses_with_5035_and_nothing_admitted(owner, monkeypatch):
    from hermes_cli import backend_retirement
    db, _session, params, started = owner
    monkeypatch.setattr(backend_retirement, "BUSY_WAIT_S", 0.3)
    release, m = threading.Event(), {"held": threading.Event()}
    holder = threading.Thread(target=_hold_lock, args=(release, m))
    holder.start()
    m["held"].wait(5)
    try:
        response = call("prompt.admit", params)
    finally:
        release.set()
        holder.join(5)
    assert response["error"]["code"] == 5035, response
    assert not started
    assert db.get_messages(params["stored_session_id"]) == []
