"""Host-wide Kanban singleton locks: retry when contended, prefer the root gateway, say who holds them.

Regression for hermes-hub 2026-11-02: a pinned profile gateway (askeve-reviewer) won every joint
restart, held .dispatcher.lock and .sync.lock for hours with half the root's max_in_progress, and the
root gateway, having lost one boot-time attempt, never retried. [daveey]
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import asyncio
import logging
from pathlib import Path

import pytest

from gateway import kanban_singleton as ks
from gateway.kanban_watchers_common import _acquire_singleton_lock, _release_singleton_lock


@pytest.fixture
def root(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "kanban").mkdir(parents=True)
    (home / "profiles" / "askeve-reviewer").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _lease(root, *, is_root, clock, name="dispatcher", grace=60.0):
    return ks.SingletonLease(root / "kanban" / f".{name}.lock", name=name, root=is_root, retry_seconds=30.0,
                             grace_seconds=grace, warn_after_seconds=300.0, clock=clock)


def test_root_detection_follows_hermes_home(root, monkeypatch):
    assert ks.is_root_gateway()
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "askeve-reviewer"))
    assert not ks.is_root_gateway()


def test_joint_restart_profile_gateway_defers_so_root_wins(root):
    """Profile gateway reaches the lock ~20 s before the root one: it must not take it in its grace window."""
    clock = Clock()
    profile = _lease(root, is_root=False, clock=clock)
    rootl = _lease(root, is_root=True, clock=clock)
    assert not profile.try_acquire() and profile.state == "deferred"
    clock.now += 20
    assert rootl.try_acquire() and rootl.state == "held"
    clock.now += 120  # grace over: the lock is simply contended now
    assert not profile.try_acquire() and profile.state == "contended"
    rootl.release()
    profile.release()


def test_profile_holder_yields_to_root_claim_and_root_takes_over(root):
    """Root gateway restarted alone while a profile gateway held the lock: the profile hands it back."""
    clock = Clock()
    profile = _lease(root, is_root=False, clock=clock, grace=0.0)
    rootl = _lease(root, is_root=True, clock=clock)
    assert profile.try_acquire()
    assert not profile.should_yield()
    assert not rootl.try_acquire() and rootl.state == "contended"  # posts the root claim
    clock.now += 10  # past the should_yield cache
    assert profile.should_yield()
    profile.release()
    assert not profile.try_acquire() and profile.state == "deferred"  # live root claim: stay off it
    assert rootl.try_acquire() and rootl.state == "held"
    assert not (root / "kanban" / ".dispatcher.lock.root-wanted").exists()
    assert not profile.root_claim_live()
    rootl.release()


def test_dead_root_claim_does_not_block_profile_gateway(root):
    clock = Clock()
    ks._write_json(root / "kanban" / ".dispatcher.lock.root-wanted", {"pid": 2 ** 22 + 12345, "token": "x"})
    profile = _lease(root, is_root=False, clock=clock, grace=0.0)
    assert profile.try_acquire() and profile.state == "held"
    assert not profile.should_yield()
    profile.release()


def test_long_contention_warns_naming_holder_pid_and_home(root, caplog):
    clock = Clock()
    holder = _lease(root, is_root=False, clock=clock, grace=0.0)
    waiter = _lease(root, is_root=True, clock=clock)
    assert holder.try_acquire()
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        assert not waiter.try_acquire()
        clock.now += 299
        assert not waiter.try_acquire()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        clock.now += 2
        assert not waiter.try_acquire()
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    import os
    assert f"pid {os.getpid()}" in warnings[0] and f"HERMES_HOME={root}" in warnings[0]
    holder.release()
    waiter.release()


def test_holder_info_names_pid_home_and_rootness(root):
    clock = Clock()
    lease = _lease(root, is_root=True, clock=clock)
    path = root / "kanban" / ".dispatcher.lock"
    assert ks.holder_info(path) is None
    assert lease.try_acquire()
    info = ks.holder_info(path)
    import os
    assert info is not None
    assert info["pid"] == os.getpid() and info["hermes_home"] == str(root) and info["root"] is True
    lease.release()
    assert ks.holder_info(path) is None


# --- watcher entry paths -------------------------------------------------------------------------


def _runner():
    from gateway import kanban_watchers as watchers
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    return runner


def test_dispatcher_watcher_retries_a_contended_lock_then_dispatches(root, monkeypatch):
    """Entry path 1: the dispatcher loop. Losing the boot race is no longer permanent."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_db as kb
    runner = _runner()
    cfg = {"auto_decompose": False, "dispatch_interval_seconds": 1}
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {"kanban": cfg}, kb, cfg))
    other, state = _acquire_singleton_lock(root / "kanban" / ".dispatcher.lock")
    assert state == "held"
    ticks, sleeps = [], []

    async def no_sleep(_delay):
        return None
    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)

    class FakeDispatcher:
        successful_scopes: list = []

        def __init__(self, *_a, **_k):
            pass

        def tick_once(self, **_k):
            ticks.append(runner._owns_kanban_dispatcher_lock())
            runner._running = False
            return []

        def ready_nonempty(self):
            return False
    monkeypatch.setattr(watchers, "_KanbanDispatcher", FakeDispatcher)

    async def between(interval):
        sleeps.append(interval)
        if len(sleeps) == 1:
            _release_singleton_lock(other)  # the other gateway exits
    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    try:
        asyncio.run(runner._kanban_dispatcher_watcher())
    finally:
        _release_singleton_lock(other)
    assert sleeps[0] == ks.DEFAULT_RETRY_SECONDS
    assert ticks == [True]
    assert runner._kanban_dispatcher_lock_handle is None  # released on stop


def test_dispatcher_watcher_profile_gateway_yields_to_root(root, monkeypatch):
    """Entry path 2: a profile gateway holding the dispatcher lock hands it to a root claimant."""
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "askeve-reviewer"))
    monkeypatch.setattr(ks, "DEFAULT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ks, "_YIELD_CACHE_SECONDS", 0.0)
    runner = _runner()
    cfg = {"auto_decompose": False, "dispatch_interval_seconds": 1}
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {"kanban": cfg}, kb, cfg))

    async def no_sleep(_delay):
        return None
    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)
    root_lease = _lease(root, is_root=True, clock=Clock())
    events = []

    class FakeDispatcher:
        successful_scopes: list = []

        def __init__(self, *_a, **_k):
            pass

        def tick_once(self, **_k):
            events.append(("tick", runner._owns_kanban_dispatcher_lock()))
            assert not root_lease.try_acquire()  # root gateway starts and posts its claim
            return []

        def ready_nonempty(self):
            return False
    monkeypatch.setattr(watchers, "_KanbanDispatcher", FakeDispatcher)

    async def between(interval):
        events.append(("sleep", runner._owns_kanban_dispatcher_lock()))
        if not runner._owns_kanban_dispatcher_lock() and root_lease.try_acquire():
            events.append(("root-held", True))
            runner._running = False
    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    try:
        asyncio.run(runner._kanban_dispatcher_watcher())
    finally:
        root_lease.release()
    assert events[0] == ("tick", True)
    assert ("root-held", True) in events
    assert events.count(("tick", True)) == 1  # no second tick after the root claim


def test_sync_watcher_profile_gateway_yields_to_root_and_waits(root, monkeypatch):
    """Entry path 3: the sync loop gives .sync.lock back to the root gateway and does not spin."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "askeve-reviewer"))
    monkeypatch.setattr(ks, "DEFAULT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ks, "_YIELD_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(sync, "sync_enabled", lambda: True)
    runner = _runner()
    root_lease = _lease(root, is_root=True, clock=Clock(), name="sync")
    loops = []

    def fake_run_forever(keep_running, interval=None, sleep=None):
        loops.append("start")
        assert keep_running()
        assert not root_lease.try_acquire()  # root gateway comes up and asks
        assert not keep_running()  # ... so the profile's loop ends at its next check
    monkeypatch.setattr(ksc, "run_forever", fake_run_forever)
    sleeps = []

    async def between(interval):
        sleeps.append(interval)
        assert root_lease.try_acquire()  # the yielded lock is the root's now
        runner._running = False
    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    try:
        asyncio.run(runner._kanban_sync_watcher())
    finally:
        root_lease.release()
    assert loops == ["start"] and sleeps == [ks.DEFAULT_RETRY_SECONDS]
