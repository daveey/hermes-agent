"""Bounded DNS observation and per-request wait ownership."""
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import retry_utils as ru
from agent.turn_offline_wait import offline_wait_budget, wait_out_offline
from agent.turn_retry_state import TurnRetryState


def test_hung_dns_observation_is_interruptible_and_threads_are_bounded(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(ru, "_dns_slots", slots)
    calls = []

    def stuck(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(10)
        return [(socket.AF_INET,)]

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    try:
        # Interrupt only after the resolver really entered its blocked native-call seam.
        assert not ru.host_resolves("example.com", is_interrupted=entered.is_set)
        assert entered.is_set()
        for _ in range(10):
            assert not ru.host_resolves("example.com", timeout=0.01)
        assert len(calls) == 1
    finally:
        release.set()
        assert slots.acquire(timeout=10)
        slots.release()


def test_dns_timeout_returns_without_waiting_for_native_lookup(monkeypatch):
    release = threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(ru, "_dns_slots", slots)

    def stuck(*args, **kwargs):
        assert release.wait(10)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    try:
        assert not ru.host_resolves("example.com", timeout=0.01)
        assert not slots.acquire(blocking=False)  # unresolved thread still owns its slot
    finally:
        release.set()
        assert slots.acquire(timeout=10)
        slots.release()


def _agent():
    return SimpleNamespace(_offline_wait_seconds=10, base_url="https://api.example.com/v1",
                           platform="desktop", log_prefix="", _interrupt_requested=False,
                           _touch_activity=Mock(), _emit_diagnostic_status=Mock(),
                           _emit_diagnostic_wait=Mock(), clear_interrupt=Mock(return_value=False))


def _wait(agent, state):
    return wait_out_offline(agent, socket.gaierror(8, "offline"), state,
                            messages=[], conversation_history=[], api_call_count=2)


def test_repeated_outages_share_one_request_budget_and_feed_watchdog(monkeypatch):
    agent, state = _agent(), TurnRetryState()
    budgets = []

    def wait(host, *, budget_s, is_interrupted, on_tick, on_wait):
        budgets.append(budget_s)
        assert not is_interrupted()
        on_tick(6)
        return ("online", 6) if len(budgets) == 1 else ("exhausted", 4)

    monkeypatch.setattr(ru, "wait_for_network", wait)
    assert _wait(agent, state) == ("continue", None)
    assert _wait(agent, state) is None
    assert _wait(agent, state) is None
    assert budgets == [10, 4]
    assert state.offline_wait_spent_s == 10
    assert any("6s awake" in c.args[0] for c in agent._touch_activity.call_args_list)
    assert any("network is back" in c.args[0] for c in agent._emit_diagnostic_status.call_args_list)


@pytest.mark.parametrize("outcome", ["already_online", "misconfigured", "disabled"])
def test_non_offline_verdict_falls_through_to_normal_retries(monkeypatch, outcome):
    monkeypatch.setattr(ru, "wait_for_network", lambda *a, **k: (outcome, 0))
    assert _wait(_agent(), TurnRetryState()) is None


@pytest.mark.parametrize("redirect", [False, True])
def test_interrupt_preserves_redirect_or_returns_abort(monkeypatch, redirect):
    from agent import turn_recovery
    agent, state = _agent(), TurnRetryState()
    agent.clear_interrupt.return_value = redirect
    monkeypatch.setattr(ru, "wait_for_network", lambda *a, **k: ("interrupted", 1))
    abort = Mock(return_value={"interrupted": True, "completed": False})
    monkeypatch.setattr(turn_recovery, "abort_turn_on_interrupt", abort)
    result = _wait(agent, state)
    assert result == (("break", None) if redirect else ("return", abort.return_value))
    assert state.restart_with_redirected_messages is redirect
    assert abort.call_count == (0 if redirect else 1)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, "900"])
def test_wait_budget_rejects_nonfinite_and_non_numeric_config(value):
    assert offline_wait_budget(SimpleNamespace(_offline_wait_seconds=value)) == 900
