"""A background review (the post-turn memory/skill review fork) holds the retirement fence.

The review runs on a daemon thread with no session turn lease and no admission reservation, so the
idle proof used to read a backend mid-review as idle: on 2026-10-28 04:38 an SSH-isolated backend's
skew retirement committed while two reviews were mid-call and the shutdown hard-interrupted both
(``reason=session_ended phase=model_wait``), losing a skill change. Now a running review keeps the
fence closed until it finishes; past ``BACKGROUND_REVIEW_WAIT_S`` it is asked to stop between API
calls (never mid-call), and only past ``BACKGROUND_REVIEW_HARD_CAP_S`` does it stop counting.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_cli import backend_retirement, web_server_idle_proof
from hermes_cli.backend_retirement import (
    BACKGROUND_REVIEW_HARD_CAP_S, BACKGROUND_REVIEW_WAIT_S, RetirementFence,
)


@pytest.fixture
def fence(monkeypatch):
    """A fresh process fence on a fake clock whose other ledgers are empty (no leases, no turns)."""
    fence = RetirementFence()
    now = [1000.0]
    monkeypatch.setattr(fence, "_now", lambda: now[0], raising=False)
    fence.clock = now
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    monkeypatch.setattr(web_server_idle_proof, "idle_proof", lambda: {"idle": True, "reason": None})
    return fence


def test_a_running_review_with_no_leases_is_not_retired_until_it_finishes(fence):
    hold = fence.begin_background_review()
    assert hold is not None
    assert fence.prepare() == {"ok": False, "idle": False}
    fence.clock[0] += BACKGROUND_REVIEW_WAIT_S - 1
    assert fence.prepare() == {"ok": False, "idle": False}
    assert not hold.wind_down.is_set()  # inside the bound: left to finish, never interrupted
    fence.end_background_review(hold)
    permit = fence.prepare()
    assert permit["ok"] is True and fence.commit(permit["token"]) == {"ok": True}


def test_past_the_wait_bound_the_review_is_asked_to_stop_between_calls_then_released(fence):
    hold = fence.begin_background_review()
    fence.clock[0] += BACKGROUND_REVIEW_WAIT_S
    assert fence.prepare() == {"ok": False, "idle": False}
    assert hold.wind_down.is_set()  # cooperative: the fork stops before its NEXT provider call
    fence.clock[0] += BACKGROUND_REVIEW_HARD_CAP_S - BACKGROUND_REVIEW_WAIT_S - 1
    assert fence.prepare() == {"ok": False, "idle": False}
    fence.clock[0] += 1
    # A review wedged past the hard cap can no longer starve retirement.
    assert fence.prepare()["ok"] is True


def test_no_review_starts_once_retirement_holds_a_permit(fence):
    permit = fence.prepare()
    assert permit["ok"] is True
    assert fence.begin_background_review() is None
    assert fence.cancel(permit["token"]) == {"ok": True}
    hold = fence.begin_background_review()
    assert hold is not None
    fence.end_background_review(hold)
    permit = fence.prepare()
    fence.commit(permit["token"])
    assert fence.begin_background_review() is None  # a retired generation never starts new reviews


def test_skew_watchdog_waits_for_a_running_review_and_does_not_interrupt_it(fence):
    from hermes_cli.web_server_skew_exit import start_code_skew_watchdog

    server = SimpleNamespace(should_exit=False)
    hold = fence.begin_background_review()
    skew = ("aaaaaaaaaa", "bbbbbbbbbb")
    thread = start_code_skew_watchdog(server, skew_fn=lambda: skew, update_probe=lambda: False,
                                      fence=fence, poll_s=0.01, max_polls=6)
    thread.join(timeout=5)
    assert server.should_exit is False
    assert not hold.wind_down.is_set()
    fence.end_background_review(hold)
    thread = start_code_skew_watchdog(server, skew_fn=lambda: skew, update_probe=lambda: False,
                                      fence=fence, poll_s=0.01, max_polls=4)
    thread.join(timeout=5)
    assert server.should_exit is True


def test_idle_probe_names_the_running_review(fence):
    from hermes_cli.web_server_idle_exit import busy_ledger

    assert busy_ledger() == ""
    hold = fence.begin_background_review()
    try:
        assert busy_ledger() == "background_review:1"
        fence.clock[0] += BACKGROUND_REVIEW_HARD_CAP_S
        assert busy_ledger() == ""
    finally:
        fence.end_background_review(hold)


def test_review_fork_stops_before_its_next_provider_call_on_wind_down():
    from agent.turn_iteration_prep import begin_iteration

    hold = backend_retirement.BackgroundReviewHold(0.0)
    agent = SimpleNamespace(
        _interrupt_requested=False, quiet_mode=True, _drain_pending_redirect=lambda: None,
        _checkpoint_mgr=SimpleNamespace(new_turn=lambda: None), _retirement_review_hold=hold,
        _budget_grace_call=False, iteration_budget=SimpleNamespace(consume=lambda: True),
        _touch_activity=lambda *_: None,
    )
    kwargs = dict(messages=[], conversation_history=[], original_user_message="review",
                  api_call_count=2, interrupted=False, _turn_exit_reason="unknown")
    assert begin_iteration(agent, **kwargs).action == "fallthrough"
    hold.wind_down.set()
    verdict = begin_iteration(agent, **kwargs)
    assert (verdict.action, verdict.interrupted, verdict._turn_exit_reason) == (
        "break", False, "review_retirement_wind_down")
    assert verdict.api_call_count == 2  # no further provider call was started


def _spawning_agent():
    from run_agent import AIAgent

    agent = MagicMock()
    agent._background_review_lock = threading.Lock()
    agent._background_review_run = None
    agent._spawn_background_review_now = AIAgent._spawn_background_review_now.__get__(agent)
    return agent


def test_spawned_review_holds_the_fence_for_its_whole_run(fence, monkeypatch):
    from agent import background_review

    started, release, seen = threading.Event(), threading.Event(), {}

    def _target():
        seen["run"] = agent._background_review_run
        started.set()
        release.wait(5)

    monkeypatch.setattr(background_review, "spawn_background_review_thread",
                        lambda *a, **k: (_target, "prompt"))
    agent = _spawning_agent()
    agent._spawn_background_review_now(messages_snapshot=[], review_skills=True)
    assert started.wait(5)
    assert seen["run"].retirement_hold is not None
    assert fence.prepare() == {"ok": False, "idle": False}
    release.set()
    for _ in range(200):
        if fence.background_review_count() == 0:
            break
        threading.Event().wait(0.01)
    assert fence.background_review_count() == 0
    assert fence.prepare()["ok"] is True


def test_review_is_skipped_cleanly_on_a_retiring_backend(fence, monkeypatch):
    from agent import background_review

    spawned = []
    monkeypatch.setattr(background_review, "spawn_background_review_thread",
                        lambda *a, **k: spawned.append(1) or (lambda: None, "prompt"))
    fence.commit(fence.prepare()["token"])
    agent = _spawning_agent()
    agent._spawn_background_review_now(messages_snapshot=[], review_skills=True)
    assert spawned == []
    assert agent._background_review_run is None  # the run token is released, not leaked
