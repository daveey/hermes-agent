"""Idle-only update pauses must never start a drain while work is active."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from gateway import cron_pause
from gateway.control_socket import GatewayControlServer, pause_gateway_for_update


@pytest.fixture
def pause_state(monkeypatch):
    runner = SimpleNamespace(
        config=object(),
        _active_work_count=Mock(return_value=0),
        request_restart=Mock(return_value=True),
    )
    preserve = Mock()
    monkeypatch.setattr(cron_pause, "preserve_cron_slots", preserve)
    monkeypatch.setattr("hermes_cli.gateway._get_restart_drain_timeout", lambda: 12.5)
    return runner, preserve


def _inline_loop():
    return SimpleNamespace(call_soon_threadsafe=lambda callback: callback())


def test_busy_idle_pause_has_no_restart_or_cron_side_effects(pause_state):
    runner, preserve = pause_state
    runner._active_work_count.return_value = 1

    result = cron_pause.pause_for_update(runner, _inline_loop(), idle_only=True)

    assert result == {"pausing": False, "already_stopping": False, "busy": True,
                      "pid": os.getpid(), "drain_timeout": 12.5}
    runner._active_work_count.assert_called_once_with()
    runner.request_restart.assert_not_called()
    preserve.assert_not_called()


def test_idle_pause_preserves_slots_before_requesting_restart(pause_state):
    runner, preserve = pause_state
    ordered = Mock()
    ordered.attach_mock(runner._active_work_count, "active_work")
    ordered.attach_mock(preserve, "preserve")
    ordered.attach_mock(runner.request_restart, "restart")

    result = cron_pause.pause_for_update(runner, _inline_loop(), idle_only=True)

    assert result == {"pausing": True, "already_stopping": False, "busy": False,
                      "pid": os.getpid(), "drain_timeout": 12.5}
    assert ordered.mock_calls == [call.active_work(), call.preserve(runner.config),
                                  call.restart(detached=False, via_service=True)]


@pytest.mark.parametrize("run_after_return", [False, True], ids=["at-deadline", "after-timeout"])
def test_expired_scheduled_callback_cannot_start_a_drain(pause_state, monkeypatch, run_after_return):
    runner, preserve = pause_state
    queued = []
    clock = SimpleNamespace(monotonic=Mock(return_value=100.0))
    event = Mock()
    monkeypatch.setattr(cron_pause, "time", clock)
    monkeypatch.setattr(cron_pause, "threading", SimpleNamespace(Event=lambda: event))

    def expire_wait(timeout):
        assert timeout == 5.0
        clock.monotonic.return_value = 105.0
        if not run_after_return:
            queued.pop()()
        return False

    event.wait.side_effect = expire_wait
    loop = SimpleNamespace(call_soon_threadsafe=queued.append)
    result = cron_pause.pause_for_update(runner, loop, idle_only=True)
    assert result["pausing"] is False
    assert result["already_stopping"] is False
    if run_after_return:
        queued.pop()()
    else:
        assert result["busy"] is True
    runner.request_restart.assert_not_called()
    preserve.assert_not_called()
    event.set.assert_called_once_with()


def test_active_count_is_checked_in_scheduled_callback(pause_state, monkeypatch):
    runner, preserve = pause_state

    def schedule(callback):
        preserve.assert_not_called()
        runner._active_work_count.assert_not_called()
        runner._active_work_count.return_value = 1
        callback()

    result = cron_pause.pause_for_update(
        runner, SimpleNamespace(call_soon_threadsafe=schedule), idle_only=True,
    )
    assert result["busy"] is True
    assert result["pausing"] is False
    preserve.assert_not_called()
    runner.request_restart.assert_not_called()


def test_legacy_pause_still_requests_restart_with_active_work(pause_state):
    runner, preserve = pause_state
    runner._active_work_count.return_value = 3

    result = cron_pause.pause_for_update(runner, _inline_loop())

    assert result["pausing"] is True
    assert result["busy"] is False
    runner._active_work_count.assert_not_called()
    preserve.assert_called_once_with(runner.config)
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("idle_only,active", [(True, 1), (True, 0), (False, 1)])
def test_pause_client_routes_over_real_socket(tmp_path, pause_state, idle_only, active):
    runner, preserve = pause_state
    runner._active_work_count.return_value = active
    verbs = []

    async def scenario():
        loop = asyncio.get_running_loop()

        def idle_handler():
            verbs.append("pause-if-idle-for-update")
            return cron_pause.pause_for_update(runner, loop, idle_only=True)

        def legacy_handler():
            verbs.append("pause-for-update")
            return cron_pause.pause_for_update(runner, loop)

        server = GatewayControlServer(home=tmp_path, verb_handlers={
            "pause-if-idle-for-update": idle_handler,
            "pause-for-update": legacy_handler,
        })
        assert await server.start()
        try:
            # Omit the flag for the legacy case to test the client's default route.
            kwargs = {"idle_only": True} if idle_only else {}
            return await asyncio.to_thread(pause_gateway_for_update, tmp_path, timeout=5.0, **kwargs)
        finally:
            await server.stop()

    result = asyncio.run(scenario())
    assert verbs == ["pause-if-idle-for-update" if idle_only else "pause-for-update"]
    assert result is not None
    refused = idle_only and active > 0
    assert result["busy"] is refused
    assert result["pausing"] is (not refused)
    assert result["already_stopping"] is False
    assert result["drain_timeout"] == 12.5
    if refused:
        preserve.assert_not_called()
        runner.request_restart.assert_not_called()
    else:
        preserve.assert_called_once_with(runner.config)
        runner.request_restart.assert_called_once_with(detached=False, via_service=True)
