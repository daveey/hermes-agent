"""Background review claims are visible to the cross-process drain apply gate."""
from unittest.mock import MagicMock

import pytest

from hermes_cli import admission_fence, backend_retirement
from hermes_cli.backend_retirement import RetirementFence

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_review_claim_blocks_drain_until_end_even_without_a_turn_lease(root):
    fence = RetirementFence()
    hold = fence.begin_background_review()
    assert hold is not None
    try:
        assert fence.active_count() == 0
        assert admission_fence.set_hold(root)["reservations"] == 1
        assert fence.begin_background_review() is None
        assert admission_fence.state(root)["reservations"] == 1
    finally:
        fence.end_background_review(hold)
        fence.end_background_review(hold)
        admission_fence.clear_hold(root)
    assert admission_fence.state(root)["reservations"] == 0
    successor = fence.begin_background_review()
    assert successor is not None
    fence.end_background_review(successor)


def test_named_profile_review_claim_uses_installation_root(root, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "writer"))
    fence = RetirementFence()
    hold = fence.begin_background_review()
    try:
        assert admission_fence.state(root)["reservations"] == 1
        assert not (root / "profiles" / "writer" / "core-update").exists()
    finally:
        fence.end_background_review(hold)


@pytest.mark.parametrize("error", [OSError("unreadable"), TimeoutError("busy")])
def test_review_admission_fails_closed(root, monkeypatch, error):
    def unavailable(_root):
        raise error
    monkeypatch.setattr(admission_fence, "reserve", unavailable)
    fence = RetirementFence()
    assert fence.begin_background_review() is None
    assert fence.background_review_count() == 0


def test_retirement_race_releases_cross_process_claim(root, monkeypatch):
    fence = RetirementFence()
    reserve = admission_fence.reserve

    def race(home):
        pending = reserve(home)
        fence._preparing = True
        return pending

    monkeypatch.setattr(admission_fence, "reserve", race)
    assert fence.begin_background_review() is None
    assert admission_fence.state(root)["reservations"] == 0


def test_failed_review_thread_start_releases_both_claims(root, monkeypatch):
    import threading
    from agent import background_review
    from run_agent import AIAgent

    fence = RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    agent = MagicMock()
    agent._background_review_lock = threading.Lock()
    agent._background_review_run = None
    monkeypatch.setattr(background_review, "spawn_background_review_thread",
                        lambda *a, **k: (lambda: None, "prompt"))
    monkeypatch.setattr(threading.Thread, "start", MagicMock(side_effect=RuntimeError("start failed")))
    with pytest.raises(RuntimeError, match="start failed"):
        AIAgent._spawn_background_review_now(agent, messages_snapshot=[], review_skills=True)
    assert admission_fence.state(root)["reservations"] == 0
    assert fence.background_review_count() == 0
    assert agent._background_review_run is None
