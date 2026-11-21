"""A core update no longer waits on a background review that is mid provider call.

A review reads the drain's pause request only at step boundaries, and holds its admission reservation for its
whole run. One long provider call therefore kept the reservation past the drain's claim hold and the update gave up
for 60 minutes (David-MBP 2026-11-20 22:54, "admission reservations busy: /Users/daveey/.hermes (1)", one
``background_review``). Now each review's watcher interrupts it mid-call once a pause request has stood
``REVIEW_UPDATE_PAUSE_GRACE_S``; the review releases its reservation and is requeued, in memory and on disk
(``agent.review_update_carry``) so it still runs after the update restarts the backend. A review the update's
admission hold refuses at spawn is queued the same way instead of dropped.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli import admission_fence, backend_retirement
from hermes_cli.backend_retirement import WIND_DOWN_RETIREMENT, WIND_DOWN_UPDATE_PAUSE, RetirementFence

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(backend_retirement, "REVIEW_UPDATE_PAUSE_GRACE_S", 0.3)
    monkeypatch.setattr(backend_retirement, "_REVIEW_PAUSE_POLL_S", 0.05)


@pytest.fixture
def fence(monkeypatch):
    fence = RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    return fence


@pytest.fixture
def queue(monkeypatch):
    from agent import review_idle_queue

    queue = review_idle_queue.ReviewIdleQueue()
    monkeypatch.setattr(queue, "_ensure_thread", lambda: None)
    monkeypatch.setattr(review_idle_queue, "QUEUE", queue)
    return queue


class _Fork:
    """A review fork mid provider call: ``hard_interrupt`` is what ends the call."""

    def __init__(self):
        self.interrupted = threading.Event()
        self.messages = []

    def hard_interrupt(self, message=None, *, tool_reason=None):
        self.messages.append((message, tool_reason))
        self.interrupted.set()


def _ask_turns_to_pause(root, *, expires_in=60.0):
    admission_fence.set_hold(root)
    admission_fence.request_pause(root, expires_at=time.time() + expires_in)


def test_a_review_mid_call_is_interrupted_once_the_pause_request_has_stood_its_grace(root, fence, fast):
    hold = fence.begin_background_review(session_id="20261026_235722_bef6c6")
    fork = _Fork()
    hold.review_agent = fork
    try:
        started = time.monotonic()
        _ask_turns_to_pause(root)
        assert fork.interrupted.wait(10), "the review's in-flight call was never interrupted"
        assert time.monotonic() - started >= 0.3 - 0.05  # not before the grace
        assert hold.wind_down.is_set() and hold.wind_down_reason == WIND_DOWN_UPDATE_PAUSE
        assert fork.messages == [("stopped for a Hermes update", "hermes update pause")]  # the survivable reason
    finally:
        fence.end_background_review(hold)
    assert admission_fence.state(root)["reservations"] == 0


def test_no_pause_request_leaves_the_review_alone(root, fence, fast):
    hold = fence.begin_background_review()
    fork = _Fork()
    hold.review_agent = fork
    try:
        assert not fork.interrupted.wait(0.8)
        assert not hold.wind_down.is_set()
    finally:
        fence.end_background_review(hold)


def test_a_request_that_lapses_inside_the_grace_does_not_interrupt(root, fence, fast):
    hold = fence.begin_background_review()
    fork = _Fork()
    hold.review_agent = fork
    try:
        _ask_turns_to_pause(root, expires_in=0.1)  # expires before the 0.3 s grace is up
        assert not fork.interrupted.wait(0.8)
    finally:
        fence.end_background_review(hold)


def test_a_retirement_wind_down_keeps_its_reason_and_is_not_interrupted(root, fence, fast):
    hold = fence.begin_background_review()
    fork = _Fork()
    hold.review_agent = fork
    hold.wind_down_reason = WIND_DOWN_RETIREMENT
    hold.wind_down.set()
    try:
        _ask_turns_to_pause(root)
        assert not fork.interrupted.wait(0.8)  # well past the 0.3 s grace
        assert hold.wind_down_reason == WIND_DOWN_RETIREMENT
    finally:
        fence.end_background_review(hold)


def test_the_watcher_thread_exits_when_the_review_ends(root, fence, fast):
    before = set(threading.enumerate())
    hold = fence.begin_background_review()
    (watcher,) = [t for t in threading.enumerate() if t not in before and t.name == "bg-review-update-pause"]
    fence.end_background_review(hold)
    watcher.join(timeout=5)
    assert not watcher.is_alive()


def test_a_review_stopped_before_its_fork_was_built_is_interrupted_when_the_fork_links(root, fence):
    from hermes_cli.backend_retirement import stop_review_for_update

    hold = fence.begin_background_review()
    try:
        assert stop_review_for_update(hold) is True  # no fork yet: wind-down only
        fork = _Fork()
        hold.review_agent = fork
        assert stop_review_for_update(hold) is True
        assert fork.interrupted.is_set()
    finally:
        fence.end_background_review(hold)


def test_a_review_that_already_ended_is_never_relabelled_update_paused(root, fence):
    """The watcher racing the review's own end: a finished review must not be requeued as update-paused."""
    from hermes_cli.backend_retirement import stop_review_for_update

    hold = fence.begin_background_review()
    fence.end_background_review(hold)
    assert stop_review_for_update(hold) is False
    assert not hold.wind_down.is_set() and hold.wind_down_reason is None


def test_a_long_provider_call_after_a_tool_is_interrupted(root, fence, fast):
    """The model-request and compression markers sit in the same in-flight table as tools: they are the long calls
    the watcher exists to cut, never a reason to wait (re-review of 7e96ec02697)."""
    from agent import update_pause_cancel

    hold = fence.begin_background_review()
    fork = _Fork()
    vars(fork)[update_pause_cancel._IN_FLIGHT] = {update_pause_cancel.MODEL_CALL_ID: time.monotonic(),
                                                  update_pause_cancel.COMPRESSION_CALL_ID: time.monotonic()}
    hold.review_agent = fork
    try:
        _ask_turns_to_pause(root)
        assert fork.interrupted.wait(10), "a long second provider call was never interrupted"
        assert hold.stopped_for_update
    finally:
        fence.end_background_review(hold)


def test_a_review_whose_last_call_finished_under_a_wind_down_is_not_requeued(root, fence, queue):
    """Wound down while a tool ran, then the review finished on its own: done, not requeued as update-paused."""
    from agent.review_idle_queue import requeue_update_paused_review
    from hermes_cli.backend_retirement import stop_review_for_update
    from agent import update_pause_cancel

    hold = fence.begin_background_review()
    fork = _Fork()
    vars(fork)[update_pause_cancel._IN_FLIGHT] = {"call_1": time.monotonic()}
    hold.review_agent = fork
    stop_review_for_update(hold)  # labelled, not interrupted: a tool was running
    fence.end_background_review(hold)  # ...and that was its last step
    assert hold.wind_down_reason == WIND_DOWN_UPDATE_PAUSE and not hold.stopped_for_update
    assert requeue_update_paused_review(_agent(), hold, "key", {"review_memory": True}, 3) is False
    assert queue.pending_count() == 0


def test_a_running_tool_is_not_cut_the_interrupt_waits_for_it(root, fence, fast):
    from agent import update_pause_cancel

    hold = fence.begin_background_review()
    fork = _Fork()
    vars(fork)[update_pause_cancel._IN_FLIGHT] = {"call_1": time.monotonic()}  # skill_manage is writing
    hold.review_agent = fork
    try:
        _ask_turns_to_pause(root)
        assert not fork.interrupted.wait(1.0), "interrupted while a tool was running"
        assert hold.wind_down.is_set()  # it still stops at the boundary after the tool
        vars(fork)[update_pause_cancel._IN_FLIGHT] = {}
        assert fork.interrupted.wait(5), "the interrupt never followed once the tool returned"
    finally:
        fence.end_background_review(hold)


def test_a_review_stuck_in_a_call_is_released_requeued_and_carried_end_to_end(root, fence, queue, fast,
                                                                               monkeypatch):
    """Through the real spawner: the provider call never returns on its own; the update's pause interrupts it."""
    from agent import background_review, review_update_carry

    agent = _spawning_agent()
    in_call, done = threading.Event(), threading.Event()

    class _BlockedFork(_Fork):
        def hard_interrupt(self, message=None, *, tool_reason=None):
            super().hard_interrupt(message, tool_reason=tool_reason)

    fork = _BlockedFork()

    def _target():
        try:
            agent._background_review_run.retirement_hold.review_agent = fork  # what _run_review_fork links
            in_call.set()
            assert fork.interrupted.wait(30), "the long call was never interrupted"
        finally:
            done.set()

    monkeypatch.setattr(background_review, "spawn_background_review_thread", lambda *a, **k: (_target, "prompt"))
    before = set(threading.enumerate())
    agent._spawn_background_review_now(messages_snapshot=[{"role": "user", "content": "hi"}], review_memory=True)
    (thread,) = [t for t in threading.enumerate() if t not in before and t.name == "bg-review"]
    assert in_call.wait(10)
    assert admission_fence.state(root)["reservations"] == 1
    _ask_turns_to_pause(root)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert admission_fence.state(root)["reservations"] == 0
    assert queue._pending["20261026_235722_bef6c6"].kwargs["_requeue_attempts"] == 1
    assert review_update_carry.take(agent) == {"review_memory": True, "review_skills": False}


# --- requeue survives the update's restart -------------------------------------------------------------------


def test_a_fork_sharing_the_session_does_not_take_the_carried_review(root):
    """Review, /btw and plugin forks share the session id; only the session's own agent runs the owed review."""
    from agent import review_update_carry as carry
    from agent.turn_finalizer import _mirror_turn_to_memory

    fork = MagicMock(session_id="20261026_235722_bef6c6", skip_background_review=False, _persist_disabled=True,
                     _delegate_depth=0)
    carry.record(fork, {"review_memory": True})
    _mirror_turn_to_memory(fork, original_user_message="btw?", final_response="answer", interrupted=False,
                           messages=[], review_memory=False, review_skills=False, vetoed=False)
    fork._spawn_background_review.assert_not_called()
    child = MagicMock(session_id="20261026_235722_bef6c6", skip_background_review=False, _persist_disabled=False,
                      _delegate_depth=1)
    _mirror_turn_to_memory(child, original_user_message="sub", final_response="done", interrupted=False,
                           messages=[], review_memory=False, review_skills=False, vetoed=False)
    child._spawn_background_review.assert_not_called()
    assert carry.take(fork) == {"review_memory": True, "review_skills": False}  # still owed to the session


def test_only_one_taker_gets_a_carried_review(root):
    from agent import review_update_carry as carry

    agent = _agent()
    carry.record(agent, {"review_memory": True})
    results = []
    threads = [threading.Thread(target=lambda: results.append(carry.take(agent))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert not list(carry._path(agent.session_id).parent.glob(".*"))  # no claim or temp file left


def test_stale_records_are_pruned_when_a_new_one_is_written(root):
    import os as _os
    from agent import review_update_carry as carry

    old = _agent("old-session")
    carry.record(old, {"review_memory": True})
    path = carry._path(old.session_id)
    stale = time.time() - carry.MAX_AGE_S - 10
    _os.utime(path, (stale, stale))
    carry.record(_agent("new-session"), {"review_memory": True})
    assert not path.exists()


def _agent(session_id="20261026_235722_bef6c6"):
    return SimpleNamespace(session_id=session_id)


def test_carry_record_round_trips_once(root):
    from agent import review_update_carry as carry

    agent = _agent()
    assert carry.take(agent) is None
    assert carry.record(agent, {"review_memory": True, "review_skills": False}) is True
    assert carry.take(agent) == {"review_memory": True, "review_skills": False}
    assert carry.take(agent) is None  # taken once


def test_carry_is_per_session_and_skips_refine_reviews(root):
    from agent import review_update_carry as carry

    carry.record(_agent("a"), {"review_skills": True})
    assert carry.take(_agent("b")) is None
    assert carry.take(_agent("a")) == {"review_memory": False, "review_skills": True}
    assert carry.record(_agent("a"), {"review_skills": True, "focus": "tidy skills"}) is False
    assert carry.record(_agent("a"), {"review_skills": True, "explicit": True}) is False
    assert carry.record(SimpleNamespace(session_id=None), {"review_skills": True}) is False


def test_a_stale_or_corrupt_carry_record_is_dropped(root, monkeypatch):
    from agent import review_update_carry as carry

    agent = _agent()
    carry.record(agent, {"review_memory": True})
    later = time.time() + carry.MAX_AGE_S + 1
    monkeypatch.setattr(carry.time, "time", lambda: later)
    assert carry.take(agent) is None
    monkeypatch.undo()
    carry._path(agent.session_id).parent.mkdir(parents=True, exist_ok=True)
    carry._path(agent.session_id).write_text("{not json")
    assert carry.take(agent) is None
    assert not carry._path(agent.session_id).exists()


def test_carry_clear_removes_the_record(root):
    from agent import review_update_carry as carry

    agent = _agent()
    carry.record(agent, {"review_memory": True})
    carry.clear(agent)
    assert carry.take(agent) is None


def test_an_update_paused_requeue_is_also_carried_to_disk(root, queue):
    from agent import review_update_carry as carry
    from agent.review_idle_queue import requeue_update_paused_review

    hold = SimpleNamespace(wind_down_reason=WIND_DOWN_UPDATE_PAUSE, stopped_for_update=True)
    agent = _agent()
    assert requeue_update_paused_review(agent, hold, "key", {"review_memory": True, "_requeue_attempts": 1}, 3)
    assert queue.pending_count() == 1
    assert carry.take(agent) == {"review_memory": True, "review_skills": False}


def test_the_sessions_next_finished_turn_runs_the_carried_review(root):
    from agent import review_update_carry as carry
    from agent.turn_finalizer import _mirror_turn_to_memory

    agent = MagicMock(session_id="20261026_235722_bef6c6", skip_background_review=False, _persist_disabled=False,
                      _delegate_depth=0)
    carry.record(agent, {"review_memory": True, "review_skills": True})
    _mirror_turn_to_memory(agent, original_user_message="hi", final_response="done", interrupted=False,
                           messages=[{"role": "user", "content": "hi"}], review_memory=False, review_skills=False,
                           vetoed=False)
    agent._spawn_background_review.assert_called_once()
    kwargs = agent._spawn_background_review.call_args.kwargs
    assert kwargs["review_memory"] is True and kwargs["review_skills"] is True
    assert carry.take(agent) is None


def test_an_interrupted_turn_keeps_the_carried_review_for_a_later_turn(root):
    from agent import review_update_carry as carry
    from agent.turn_finalizer import _mirror_turn_to_memory

    agent = MagicMock(session_id="s1", skip_background_review=False, _persist_disabled=False, _delegate_depth=0)
    carry.record(agent, {"review_memory": True})
    _mirror_turn_to_memory(agent, original_user_message="hi", final_response="", interrupted=True,
                           messages=[], review_memory=False, review_skills=False, vetoed=False)
    agent._spawn_background_review.assert_not_called()
    assert carry.take(agent) == {"review_memory": True, "review_skills": False}


# --- a review refused by the update hold is queued, not dropped ------------------------------------------------


def _spawning_agent(session_id="20261026_235722_bef6c6"):
    from run_agent import AIAgent

    agent = MagicMock()
    agent.session_id = session_id
    agent._background_review_lock = threading.Lock()
    agent._background_review_run = None
    agent._REVIEW_REQUEUE_MAX_ATTEMPTS = AIAgent._REVIEW_REQUEUE_MAX_ATTEMPTS
    agent._spawn_background_review_now = AIAgent._spawn_background_review_now.__get__(agent)
    return agent


def test_a_review_spawned_under_the_update_hold_is_queued_and_carried(root, fence, queue):
    from agent import review_update_carry as carry

    _ask_turns_to_pause(root)
    agent = _spawning_agent()
    agent._spawn_background_review_now(messages_snapshot=[{"role": "user", "content": "hi"}], review_memory=True)
    assert admission_fence.state(root)["reservations"] == 0
    pending = queue._pending["20261026_235722_bef6c6"]
    assert pending.kwargs["review_memory"] is True and pending.kwargs["_requeue_attempts"] == 1
    assert carry.take(agent) == {"review_memory": True, "review_skills": False}


def test_a_review_out_of_attempts_under_the_hold_is_dropped(root, fence, queue):
    _ask_turns_to_pause(root)
    agent = _spawning_agent()
    agent._spawn_background_review_now(messages_snapshot=[], review_memory=True,
                                       _requeue_attempts=agent._REVIEW_REQUEUE_MAX_ATTEMPTS)
    assert queue.pending_count() == 0


def test_a_retiring_backend_still_skips_the_review(root, fence, queue, monkeypatch):
    monkeypatch.setattr(fence, "retiring", lambda: True)
    _ask_turns_to_pause(root)
    agent = _spawning_agent()
    agent._spawn_background_review_now(messages_snapshot=[], review_memory=True)
    assert queue.pending_count() == 0
