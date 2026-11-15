"""A waiter born after resume must respect the host's remaining awake grace."""

import pytest

import hermes_state_wake_grace as grace


@pytest.fixture(autouse=True)
def fresh_tracker(monkeypatch):
    monkeypatch.setattr(grace, "suspended_total_s", lambda: 1000.0)
    monkeypatch.setattr(grace, "_awake_now", lambda: 500.0)
    grace._reset_for_tests()
    yield
    grace._reset_for_tests()


@pytest.mark.parametrize("wake,expected", [
    (490.0, True), (300.0, False), (410.0, False),
    (410.001, True), (None, False), (501.0, False),
])
def test_fresh_tracker_uses_host_wake(monkeypatch, wake, expected):
    monkeypatch.setattr(grace, "host_wake_awake_s", lambda: wake, raising=False)
    assert grace.recently_woke() is expected
    assert grace.lapsed_row_reclaimable("live", 1.0, 2.0) is not expected


def test_host_grace_expires_without_restarting_on_each_poll(monkeypatch):
    monkeypatch.setattr(grace, "host_wake_awake_s", lambda: 490.0, raising=False)
    assert grace.recently_woke()
    monkeypatch.setattr(grace, "_awake_now", lambda: 580.0)
    assert not grace.recently_woke()


def test_host_signal_does_not_require_process_suspend_baseline(monkeypatch):
    monkeypatch.setattr(grace, "suspended_total_s", lambda: None)
    monkeypatch.setattr(grace, "host_wake_awake_s", lambda: 490.0, raising=False)
    assert grace.recently_woke()


def test_absent_host_signal_retains_process_suspend_detection(monkeypatch):
    monkeypatch.setattr(grace, "host_wake_awake_s", lambda: None, raising=False)
    assert not grace.recently_woke()
    monkeypatch.setattr(grace, "suspended_total_s", lambda: 1600.0)
    assert grace.recently_woke()
    monkeypatch.setattr(grace, "_awake_now", lambda: 590.0)
    assert not grace.recently_woke()
