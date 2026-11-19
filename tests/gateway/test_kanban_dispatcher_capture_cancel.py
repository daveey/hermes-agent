"""Dispatcher tenure must close a late capture session and drop the lease.

``asyncio.to_thread`` keeps boot/close running after the waiter is cancelled, so
moving boot into ``try`` is not enough: the late return value still has to close,
and ``lease.release`` must run even when close raises or is cancelled.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace
import threading

import pytest

from gateway import kanban_watchers as watchers

_MARKER = ContextVar("kanban_capture_process_service", default=None)


class FakeSession:
    def __init__(self):
        self.open = True
        self.close_calls = 0
        self.close_contexts = []
        self.close_threads = []

    def close(self):
        self.close_calls += 1
        self.close_contexts.append(_MARKER.get())
        self.close_threads.append(threading.current_thread().ident)
        self.open = False


class RaisingCloseSession(FakeSession):
    def close(self):
        super().close()
        raise RuntimeError("close failed")


class DummyHealth:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass

    def invalidate(self):
        pass

    def publish(self, *args, **kwargs):
        pass


class FakeDispatcher:
    successful_scopes = ()

    def __init__(self, kb, settings):
        self.runner = None

    def tick_once(self, *, suppress_spawn=False):
        return []

    def ready_nonempty(self):
        return False

    def auto_decompose_tick(self, per_tick):
        return 0


def _lease():
    released = []
    handle = object()
    lease = SimpleNamespace(
        should_yield=lambda: False,
        handle=handle,
        release=lambda: released.append(True),
    )
    return lease, released


def _runner(lease):
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    runner._kanban_dispatcher_lease = lease
    runner._kanban_dispatcher_lock_handle = lease.handle
    return runner


def _patch_loop(monkeypatch, runner, boot=None, dispatcher=None):
    monkeypatch.setattr("gateway.kanban_dispatcher_health.DispatcherHealth", DummyHealth)
    if boot is not None:
        monkeypatch.setattr("hermes_cli.kanban_fairness_activation.boot", boot)
    monkeypatch.setattr(watchers, "_KanbanDispatcher", dispatcher or FakeDispatcher)

    original_sleep = asyncio.sleep

    async def skip_startup_delay(delay, result=None):
        if delay == 5:
            return result
        return await original_sleep(delay, result)

    monkeypatch.setattr(watchers.asyncio, "sleep", skip_startup_delay)

    async def stop_after_tick(_interval):
        runner._running = False

    monkeypatch.setattr(runner, "_sleep_until_tick_or_sync_wake", stop_after_tick)
    monkeypatch.setattr("hermes_cli.kanban_db_dispatch.reap_worker_zombies", list)
    monkeypatch.setattr("hermes_cli.kanban_hub_cpu_alert.check", lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_sync_outbox_age.check", lambda: None)
    monkeypatch.setattr("agent.estop.check_paused", lambda *a, **k: False)
    monkeypatch.setattr("agent.core_update_drain.check_drain_hold", lambda *a, **k: False)


def _held(runner, lease):
    return runner._kanban_dispatch_while_held(
        SimpleNamespace(interval=1.0), lambda: {"kanban": {"auto_decompose": False}}, None, lease)


async def _expect_cancelled(task):
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.platforms("posix")
def test_serving_then_dispatcher_tenure_does_not_close_borrowed_epoch(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_activation as activation
    from hermes_cli import kanban_fairness_runtime as runtime
    from hermes_cli.kanban_fairness_store import load
    from hermes_cli.kanban_turn_urgency import read_owners

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {"kanban": {"fairness_capture": {
        "enabled": True, "capture_id": "fixture", "directory": str(tmp_path / "capture"),
    }}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    activation.arm(config, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    lease, released = _lease()
    runner = _runner(lease)
    _patch_loop(monkeypatch, runner)
    close_calls = []

    with activation.serving(config, home=tmp_path) as session:
        assert session is not None
        original_close = session.close

        def close():
            close_calls.append(True)
            original_close()

        monkeypatch.setattr(session, "close", close)
        asyncio.run(_held(runner, lease))
        assert released == [True]
        assert runner._kanban_dispatcher_lock_handle is None
        assert close_calls == [], "dispatcher closed an epoch owned by serving"
        assert not session.stopping.is_set()
        assert activation.start(config, home=tmp_path) is session
        assert runtime.enabled(board="default")
        assert read_owners(board="default", now_s=150, path=tmp_path / "missing") == set()
    # The serving creator, not the borrowing tenure, owns shutdown.
    session.activation_monitor.join(3)
    assert close_calls == [True]
    assert session.stopping.is_set()
    assert not runtime.enabled(board="default")
    records = load(session.out)["records"]
    assert any(row["kind"] == "owner_generation" and row["payload"]["outcome"] == "missing"
               for row in records)


def test_cancel_during_background_boot_closes_late_session_and_releases_lease(monkeypatch):
    entered = threading.Barrier(2)
    proceed = threading.Barrier(2)
    session = FakeSession()
    boot_threads = []
    boot_contexts = []

    def boot(config=None, *, home=None):
        boot_threads.append(threading.current_thread().ident)
        boot_contexts.append(_MARKER.get())
        entered.wait(timeout=5)
        proceed.wait(timeout=5)
        return session

    lease, released = _lease()
    runner = _runner(lease)
    _patch_loop(monkeypatch, runner, boot)
    caller = threading.get_ident()

    async def run():
        _MARKER.set("caller")
        task = asyncio.create_task(_held(runner, lease))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.gather(_expect_cancelled(task), asyncio.to_thread(proceed.wait, 5))

    asyncio.run(run())
    assert boot_threads and boot_threads[0] != caller
    assert boot_contexts == [None]
    assert session.close_calls == 1
    assert session.close_contexts == [None]
    assert session.close_threads and session.close_threads[0] != caller
    assert not session.open
    assert released == [True]
    assert runner._kanban_dispatcher_lock_handle is None


def test_boot_raising_releases_lease(monkeypatch):
    def boot(config=None, *, home=None):
        raise RuntimeError("boot failed")

    lease, released = _lease()
    runner = _runner(lease)
    _patch_loop(monkeypatch, runner, boot)

    with pytest.raises(RuntimeError, match="boot failed"):
        asyncio.run(_held(runner, lease))

    assert released == [True]
    assert runner._kanban_dispatcher_lock_handle is None


def test_close_raising_releases_lease_and_session_not_open(monkeypatch):
    session = RaisingCloseSession()

    def boot(config=None, *, home=None):
        return session

    lease, released = _lease()
    runner = _runner(lease)
    _patch_loop(monkeypatch, runner, boot)

    with pytest.raises(RuntimeError, match="close failed"):
        asyncio.run(_held(runner, lease))
    assert session.close_calls == 1
    assert not session.open
    assert released == [True]
    assert runner._kanban_dispatcher_lock_handle is None


def test_cancel_during_close_releases_lease_and_session_not_open(monkeypatch):
    entered = threading.Barrier(2)
    proceed = threading.Barrier(2)
    session = FakeSession()

    def boot(config=None, *, home=None):
        return session

    def close():
        entered.wait(timeout=5)
        proceed.wait(timeout=5)
        FakeSession.close(session)

    session.close = close
    lease, released = _lease()
    runner = _runner(lease)
    _patch_loop(monkeypatch, runner, boot)

    async def run():
        task = asyncio.create_task(_held(runner, lease))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.gather(_expect_cancelled(task), asyncio.to_thread(proceed.wait, 5))

    asyncio.run(run())
    assert session.close_calls == 1
    assert not session.open
    assert released == [True]
    assert runner._kanban_dispatcher_lock_handle is None
