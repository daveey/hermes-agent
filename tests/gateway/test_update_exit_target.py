"""A core update's checkpoint promises the updater the gateway process EXITS within its drain timeout.

hermes-hub 2026-11-13 01:58-01:59Z: the checkpoint was acked at 01:58:17 (drain_timeout 70s), stop()
finished at 01:58:45, but the post-stop tail (control socket, cron/housekeeping thread waits, MCP) ran
44.5s more: the process exited at 01:59:29, 2s after the updater's 70s wait, so the update was deferred
and hermes-sync cooled off 60 min. The tail's waits are now clamped to the checkpoint's exit target.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from unittest.mock import MagicMock

import pytest

from gateway import exit_deadline
import gateway.run as gateway_run
from tests.waiting import wait_until


@pytest.fixture(autouse=True)
def _clean_deadline():
    exit_deadline._reset_for_tests()
    yield
    exit_deadline._done.set()  # a post-tail watcher still waiting stands down
    for watcher in [t for t in threading.enumerate() if t.name == exit_deadline.POST_TAIL_THREAD_NAME]:
        watcher.join(5)
    exit_deadline._reset_for_tests()


def test_exit_target_clamps_waits_without_a_deadline():
    assert exit_deadline.clamp_wait(65.0) == 65.0
    exit_deadline.set_exit_target(time.monotonic() + 20.0, "test")
    assert 12.0 <= exit_deadline.clamp_wait(65.0) <= 13.0  # 20 - TAIL_RESERVE_S
    exit_deadline.set_exit_target(None)
    assert exit_deadline.clamp_wait(65.0) == 65.0


def test_exit_target_and_deadline_take_the_earlier():
    now = time.monotonic()
    exit_deadline._state.update(deadline_mono=now + 50.0)
    exit_deadline.set_exit_target(now + 30.0)
    assert 29.0 <= exit_deadline.remaining_s() <= 30.0
    exit_deadline.set_exit_target(now + 80.0)
    assert 49.0 <= exit_deadline.remaining_s() <= 50.0


@pytest.mark.asyncio
async def test_tail_exits_by_the_update_exit_target(monkeypatch):
    """A cron ticker blocked past cron_stop held the tail up to its 65s default."""
    cron_stop = threading.Event()
    hang = threading.Event()
    cron = threading.Thread(target=hang.wait, args=(30,), daemon=True)  # ignores cron_stop
    cron.start()
    housekeeping = threading.Thread(target=cron_stop.wait, daemon=True)
    housekeeping.start()
    watcher_stop = threading.Event()
    watcher = threading.Thread(target=watcher_stop.wait, daemon=True)
    watcher.start()

    async def fake_mcp_shutdown(*_args, **_kwargs):
        return True

    monkeypatch.setattr(gateway_run, "_shutdown_mcp_servers_nonblocking", fake_mcp_shutdown)
    monkeypatch.setattr(gateway_run, "_stop_cron_provider", lambda provider: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive", lambda: None)
    runner = MagicMock(should_exit_with_failure=True, exit_reason="test stop", exit_code=None)

    from gateway.run_bootstrap import _start_gateway_shutdown_tail
    exit_deadline.set_exit_target(time.monotonic() + exit_deadline.TAIL_RESERVE_S + 1.0, "test")
    started = time.monotonic()
    await asyncio.wait_for(_start_gateway_shutdown_tail(
        runner, None, cron_stop, object(), cron, housekeeping, watcher_stop, watcher, [False]), timeout=15)
    elapsed = time.monotonic() - started
    hang.set()
    assert elapsed < 4.0


@pytest.mark.asyncio
async def test_tail_bounds_the_control_socket_stop(monkeypatch):
    """The control socket stop is given a bounded connection wait (it was unbounded)."""
    seen = {}

    class Control:
        async def stop(self, close_wait_s=None):
            seen["close_wait_s"] = close_wait_s

    cron_stop = threading.Event()
    threads = [threading.Thread(target=cron_stop.wait, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    watcher_stop = threading.Event()
    watcher = threading.Thread(target=watcher_stop.wait, daemon=True)
    watcher.start()

    async def fake_mcp_shutdown(*_args, **_kwargs):
        return True

    monkeypatch.setattr(gateway_run, "_shutdown_mcp_servers_nonblocking", fake_mcp_shutdown)
    monkeypatch.setattr(gateway_run, "_stop_cron_provider", lambda provider: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive", lambda: None)
    runner = MagicMock(should_exit_with_failure=True, exit_reason="test stop", exit_code=None)

    from gateway.run_bootstrap import _start_gateway_shutdown_tail
    await _start_gateway_shutdown_tail(
        runner, Control(), cron_stop, object(), threads[0], threads[1], watcher_stop, watcher, [False])
    from gateway.run_bootstrap import _CONTROL_STOP_TIMEOUT_S
    assert seen["close_wait_s"] == _CONTROL_STOP_TIMEOUT_S


@pytest.mark.platforms("posix")
def test_control_socket_stop_does_not_wait_on_a_held_connection(tmp_path):
    """Server.wait_closed() waits for every open connection; one held by a slow handler held the exit."""
    from gateway.control_socket import GatewayControlServer, query_gateway_control
    home = tmp_path / "h"
    home.mkdir()
    release = threading.Event()
    entered = threading.Event()

    def slow():
        entered.set()
        release.wait(20)
        return {"ok": True}

    async def scenario():
        server = GatewayControlServer(home, verb_handlers={"status": slow})
        assert await server.start()
        loop = asyncio.get_running_loop()
        client = loop.run_in_executor(None, lambda: query_gateway_control(home, "status", timeout=20))
        assert await asyncio.to_thread(entered.wait, 10)  # the request is in the handler, its connection open
        started = time.monotonic()
        try:
            await server.stop(close_wait_s=0.5)
            return time.monotonic() - started
        finally:
            release.set()
            await asyncio.gather(client, return_exceptions=True)

    elapsed = asyncio.run(scenario())
    assert elapsed < 3.0


# --- After the tail: asyncio.run's own shutdown and the outer finally (PR #1840 review) ---------------
# The exit target bounded only the tail's cooperative waits. asyncio.run's shutdown (cancelling leftover
# tasks, shutdown_default_executor: up to 300s on a wedged executor thread) and start_gateway's outer
# finally then ran unbounded until the hard exit deadline. Once the tail has resolved its verdict, a
# watcher takes the graceful exit funnel by target + POST_TAIL_GRACE_S.


def _fake_tail_env(monkeypatch, exits):
    cron_stop = threading.Event()
    threads = [threading.Thread(target=cron_stop.wait, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    watcher_stop = threading.Event()
    watcher = threading.Thread(target=watcher_stop.wait, daemon=True)
    watcher.start()

    async def fake_mcp_shutdown(*_args, **_kwargs):
        return True

    def fake_exit(code):
        exits.append((code, time.monotonic()))

    monkeypatch.setattr(gateway_run, "_shutdown_mcp_servers_nonblocking", fake_mcp_shutdown)
    monkeypatch.setattr(gateway_run, "_stop_cron_provider", lambda provider: None)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", fake_exit)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive", lambda: None)
    return (cron_stop, object(), threads[0], threads[1], watcher_stop, watcher)


def _post_tail_watchers():
    return [t for t in threading.enumerate() if t.name == exit_deadline.POST_TAIL_THREAD_NAME]


@pytest.mark.parametrize(("runner_kwargs", "code"), [
    (dict(should_exit_with_failure=True, exit_code=None), 1),
    (dict(should_exit_with_failure=False, exit_code=75), 75),  # a service restart raises SystemExit(75)
])
def test_post_tail_bound_takes_the_exit_with_the_tail_verdict(monkeypatch, runner_kwargs, code):
    exits: list = []
    env = _fake_tail_env(monkeypatch, exits)
    monkeypatch.setattr(exit_deadline, "POST_TAIL_GRACE_S", 0.2)
    exit_deadline._state.update(started_mono=time.monotonic())  # armed: phase marks are recorded
    exit_deadline.set_exit_target(time.monotonic() + 0.3, "test")
    runner = MagicMock(exit_reason="test stop", **runner_kwargs)

    from gateway.run_bootstrap import _start_gateway_shutdown_tail
    with contextlib.suppress(SystemExit):
        asyncio.run(_start_gateway_shutdown_tail(runner, None, *env, [False]))
    started = time.monotonic()
    wait_until(lambda: exits, timeout=5.0, what="the post-tail bound to take the exit")
    assert exits[0][0] == code
    assert exits[0][1] - started < 2.0
    assert "post_tail_bound" in exit_deadline.phases()


def test_post_tail_bound_needs_an_exit_target(monkeypatch):
    exits: list = []
    env = _fake_tail_env(monkeypatch, exits)
    runner = MagicMock(exit_reason="test stop", should_exit_with_failure=True, exit_code=None)

    from gateway.run_bootstrap import _start_gateway_shutdown_tail
    asyncio.run(_start_gateway_shutdown_tail(runner, None, *env, [False]))
    assert not _post_tail_watchers()  # no checkpoint promise: the hard exit deadline alone covers it
    assert exit_deadline.bound_post_tail(1, exits.append) is False
    assert not exits


def test_post_tail_bound_stands_down_once_the_funnel_disarmed():
    exits: list = []
    exit_deadline.set_exit_target(time.monotonic() + 0.2, "test")
    assert exit_deadline.bound_post_tail(0, exits.append, grace_s=0.1)
    assert exit_deadline.disarm()  # the main thread reached the exit funnel first
    for watcher in _post_tail_watchers():
        watcher.join(5)
    assert not _post_tail_watchers()
    assert not exits


def test_post_tail_bound_cuts_a_wedged_default_executor_join():
    """asyncio.run joins the default executor (up to 300s) after the coroutine returns."""
    exits: list = []
    release = threading.Event()

    async def main():
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, release.wait, 30)  # a wedged executor thread
        exit_deadline.set_exit_target(time.monotonic() + 0.3, "test")
        exit_deadline.bound_post_tail(0, lambda code: exits.append((code, time.monotonic())), grace_s=0.2)

    runner = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
    started = time.monotonic()
    runner.start()
    try:
        wait_until(lambda: exits, timeout=5.0, what="the post-tail bound to take the exit")
        assert runner.is_alive()  # asyncio.run is still joining the executor: the bound did not wait for it
        assert exits[0][0] == 0 and exits[0][1] - started < 2.0
    finally:
        release.set()
        runner.join(10)


def test_exit_funnel_parks_while_the_post_tail_bound_owns_the_exit():
    exit_deadline._state["exit_owner"] = -1  # another thread took the exit
    assert exit_deadline.disarm() is False
    assert not exit_deadline._done.is_set()
    exit_deadline._state.pop("exit_owner")
    assert exit_deadline.disarm() is True


def test_exit_funnel_parks_the_main_thread_once_the_bound_owns_the_exit():
    exit_deadline._state["exit_owner"] = -1  # the post-tail bound's thread is running the funnel
    funnel = threading.Thread(target=exit_deadline.enter_exit_funnel, daemon=True)
    funnel.start()
    funnel.join(0.5)
    assert funnel.is_alive()  # parked: no second release/stamp/os._exit from the main thread
    assert not exit_deadline._done.is_set()


def test_exit_funnel_marks_its_phase(monkeypatch):
    exit_deadline._state.update(started_mono=time.monotonic() - 2.0)
    monkeypatch.setattr(gateway_run, "_best_effort", lambda step, *a, **k: None)
    monkeypatch.setattr(gateway_run.os, "_exit", lambda code: None)
    gateway_run._exit_after_graceful_shutdown(0)
    assert 1.5 <= exit_deadline.phases()["exit_funnel"] <= 5.0


def test_slow_tail_log_carries_a_tail_start_mark(monkeypatch, caplog):
    """tail_s counted from the tail's start, the phase marks from the stop's: tail_start ties them."""
    exits: list = []
    env = _fake_tail_env(monkeypatch, exits)
    import gateway.run_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "_SLOW_TAIL_LOG_S", 0.0)
    exit_deadline._state.update(started_mono=time.monotonic() - 3.0)
    runner = MagicMock(exit_reason="test stop", should_exit_with_failure=True, exit_code=None)
    with caplog.at_level("WARNING", logger="gateway.run"):
        asyncio.run(bootstrap._start_gateway_shutdown_tail(runner, None, *env, [False]))
    marks = exit_deadline.phases()
    assert 2.5 <= marks["tail_start"] <= marks["tail_done"]
    assert any("tail_start" in rec.getMessage() for rec in caplog.records)
