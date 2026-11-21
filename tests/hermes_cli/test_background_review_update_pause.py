"""A core-update drain's pause request winds a running background review down.

A review holds an admission reservation for its whole run, and hermes-sync's drain window opens its
worker reclaim only once no reservation is held. The review used to ignore the window's pause request
(its only stop signal was process retirement, after 300 s), so one long review outlasted the drain's
3-minute claim hold and the update gave up (David-MBP 2026-11-13 05:25, incident 51354-1794576303896).
Now the review reads the request at every step boundary: it stops before its next provider call, releases the
reservation and is requeued to run after the update. A call still in flight once the request has stood
``REVIEW_UPDATE_PAUSE_GRACE_S`` is interrupted by the review's watcher (test_background_review_update_interrupt.py).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli import admission_fence, backend_retirement
from hermes_cli.backend_retirement import RetirementFence

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fence(monkeypatch):
    fence = RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    return fence


@pytest.fixture
def queue(monkeypatch):
    from agent import review_idle_queue

    queue = review_idle_queue.ReviewIdleQueue()
    monkeypatch.setattr(queue, "_ensure_thread", lambda: None)  # the tests drive dispatch themselves
    monkeypatch.setattr(review_idle_queue, "QUEUE", queue)
    return queue


def _review_agent(hold):
    return SimpleNamespace(
        _interrupt_requested=False, quiet_mode=True, _drain_pending_redirect=lambda: None,
        _checkpoint_mgr=SimpleNamespace(new_turn=lambda: None), _retirement_review_hold=hold,
        _budget_grace_call=False, iteration_budget=SimpleNamespace(consume=lambda: True),
        _touch_activity=lambda *_: None,
    )


def _step(agent, calls=2):
    from agent.turn_iteration_prep import begin_iteration

    return begin_iteration(agent, messages=[], conversation_history=[], original_user_message="review",
                           api_call_count=calls, interrupted=False, _turn_exit_reason="unknown")


def _drain_asks_turns_to_pause(root, *, expires_in=60.0):
    """What hermes-sync's drain window does: hold admission, then write the pause request."""
    admission_fence.set_hold(root)
    admission_fence.request_pause(root, expires_at=time.time() + expires_in)


def test_review_stops_before_its_next_provider_call_once_the_drain_asks_turns_to_pause(root, fence):
    hold = fence.begin_background_review()
    agent = _review_agent(hold)
    try:
        assert _step(agent).action == "fallthrough"  # no request: the review goes on
        _drain_asks_turns_to_pause(root)
        verdict = _step(agent)
        assert (verdict.action, verdict.interrupted, verdict._turn_exit_reason) == (
            "break", False, "review_update_pause_wind_down")
        assert verdict.api_call_count == 2  # no further provider call was started
        assert hold.wind_down.is_set()
    finally:
        fence.end_background_review(hold)
    assert admission_fence.state(root)["reservations"] == 0


def test_a_request_without_the_hold_or_past_its_expiry_does_not_stop_the_review(root, fence):
    hold = fence.begin_background_review()
    agent = _review_agent(hold)
    try:
        admission_fence.request_pause(root, expires_at=time.time() + 60)  # a crashed drainer's leftover: no hold
        assert _step(agent).action == "fallthrough"
        admission_fence.set_hold(root)
        admission_fence.request_pause(root, expires_at=time.time() - 1)
        assert _step(agent).action == "fallthrough"
        assert not hold.wind_down.is_set()
    finally:
        fence.end_background_review(hold)


def test_retirement_wind_down_keeps_its_own_exit_reason(root, fence):
    hold = fence.begin_background_review()
    try:
        hold.wind_down.set()
        assert _step(_review_agent(hold))._turn_exit_reason == "review_retirement_wind_down"
    finally:
        fence.end_background_review(hold)


def test_a_review_that_starts_while_the_hold_is_set_is_still_refused(root, fence):
    _drain_asks_turns_to_pause(root)
    assert fence.begin_background_review() is None  # kept behaviour (passes before the fix too)
    assert admission_fence.state(root)["reservations"] == 0
    assert fence.background_review_count() == 0


def _spawning_agent(session_id="20261026_170127_f07c32"):
    from run_agent import AIAgent

    agent = MagicMock()
    agent.session_id = session_id
    agent._background_review_lock = threading.Lock()
    agent._background_review_run = None
    agent._REVIEW_REQUEUE_MAX_ATTEMPTS = AIAgent._REVIEW_REQUEUE_MAX_ATTEMPTS
    agent._spawn_background_review_now = AIAgent._spawn_background_review_now.__get__(agent)
    agent._maybe_requeue_preempted_review = AIAgent._maybe_requeue_preempted_review.__get__(agent)
    return agent


def _spawn_stepping_review(agent, monkeypatch, *, calls, in_call, finish_call, done, **spawn_kwargs):
    """Spawn a review through the real ``_spawn_background_review_now`` whose fork loops over the real step
    boundary: each pass that falls through is one provider call (it waits in ``finish_call``)."""
    from agent import background_review

    def _target():
        try:
            fork = _review_agent(agent._background_review_run.retirement_hold)
            while len(calls) < 6 and _step(fork, calls=len(calls)).action == "fallthrough":
                calls.append(time.monotonic())
                in_call.set()
                assert finish_call.wait(10)
                finish_call.clear()
        finally:
            done.set()

    monkeypatch.setattr(background_review, "spawn_background_review_thread", lambda *a, **k: (_target, "prompt"))
    before = set(threading.enumerate())
    agent._spawn_background_review_now(messages_snapshot=[{"role": "user", "content": "hi"}], review_skills=True,
                                       **spawn_kwargs)
    (thread,) = [t for t in threading.enumerate() if t not in before and t.name == "bg-review"]
    return thread


def _join(thread):
    """The review thread's exit is the one point where its whole wind-down has happened (target, reservation
    release, requeue or drop, and the log line written after the enqueue); polling any single effect races
    the rest on a starved runner."""
    thread.join(timeout=30)
    assert not thread.is_alive(), "background review thread never exited"


def test_gateway_review_winds_down_releases_its_reservation_and_is_requeued(root, fence, queue, monkeypatch, caplog):
    """The backend path end to end: a review mid-run when the drain window asks turns to pause."""
    calls, in_call, finish_call, done = [], threading.Event(), threading.Event(), threading.Event()
    agent = _spawning_agent()
    caplog.set_level(logging.INFO)
    thread = _spawn_stepping_review(agent, monkeypatch, calls=calls, in_call=in_call, finish_call=finish_call,
                                    done=done)
    assert in_call.wait(30)  # provider call #1 is in flight
    assert admission_fence.state(root, details=True)["reservations"] == 1
    _drain_asks_turns_to_pause(root)
    assert admission_fence.state(root)["reservations"] == 1  # inside the grace the call is not cut: the claim stands
    finish_call.set()
    _join(thread)
    assert len(calls) == 1  # it stopped at the boundary: provider call #2 never started
    assert admission_fence.state(root)["reservations"] == 0  # what the drain's worker reclaim waits for
    assert fence.background_review_count() == 0
    pending = queue._pending["20261026_170127_f07c32"]
    assert pending.kwargs["review_skills"] is True and pending.kwargs["_requeue_attempts"] == 1
    assert any("stopped for a Hermes update" in r.getMessage() and "requeued" in r.getMessage()
               for r in caplog.records)


def test_requeued_review_waits_for_the_update_hold_then_dispatches(root, queue, monkeypatch):
    monkeypatch.setattr(queue, "_server_idle", lambda: True)
    monkeypatch.setattr(queue, "_quiet_for", lambda: 3600.0)
    agent = MagicMock()
    queue.enqueue(agent, "s", {"messages_snapshot": []})
    admission_fence.set_hold(root)
    assert queue._pop_dispatchable() is None  # a spawn under the hold would be refused and the review lost
    assert queue.pending_count() == 1
    admission_fence.clear_hold(root)
    item = queue._pop_dispatchable()
    assert item is not None and item.session_key == "s"


def test_review_out_of_requeues_is_dropped_with_a_log_line(root, fence, queue, monkeypatch, caplog):
    calls, in_call, finish_call, done = [], threading.Event(), threading.Event(), threading.Event()
    agent = _spawning_agent()
    caplog.set_level(logging.INFO)
    thread = _spawn_stepping_review(agent, monkeypatch, calls=calls, in_call=in_call, finish_call=finish_call,
                                    done=done, _requeue_attempts=agent._REVIEW_REQUEUE_MAX_ATTEMPTS)
    assert in_call.wait(30)
    _drain_asks_turns_to_pause(root)
    finish_call.set()
    _join(thread)
    assert fence.background_review_count() == 0
    assert queue.pending_count() == 0
    assert any("stopped for a Hermes update" in r.getMessage() and "dropped" in r.getMessage()
               for r in caplog.records)
    assert admission_fence.state(root)["reservations"] == 0


def test_state_details_names_the_reviews_reservation(root, fence):
    hold = fence.begin_background_review(session_id="20261026_170127_f07c32")
    try:
        state = admission_fence.state(root, details=True)
        assert state["version"] == 1 and state["reservations"] == 1  # the v1 count protocol is unchanged
        assert state["reservation_ids"] == [hold.reservation.path.name]
        (record,) = state["reservation_records"]
        assert record["id"] == hold.reservation.path.name
        assert record["purpose"] == "background_review"
        assert record["session"] == "20261026_170127_f07c32"
        assert record["pid"] == os.getpid()
        assert 0 <= record["age_s"] < 60
        assert "reservation_records" not in admission_fence.state(root)
    finally:
        fence.end_background_review(hold)


def test_state_details_reports_an_unlabelled_reservation_without_failing(root):
    pending = admission_fence.reserve(root)  # an older core's reservation: an empty file
    try:
        (record,) = admission_fence.state(root, details=True)["reservation_records"]
        assert record["id"] == pending.path.name
        assert record["purpose"] is None and record["pid"] is None and record["session"] is None
        assert record["age_s"] >= 0
        pending.path.write_text("not json")
        (record,) = admission_fence.state(root, details=True)["reservation_records"]
        assert record["purpose"] is None
    finally:
        pending.close()
