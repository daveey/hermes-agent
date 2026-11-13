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
    assert sleeps[0] == ks.ROOT_CLAIM_RETRY_SECONDS  # the root gateway asked the holder to yield: retry soon
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


def test_sync_watcher_old_tenure_keep_running_stays_false_after_yield(root, monkeypatch):
    """After a yield the lease is idle/deferred again, so should_yield() is False. The old tenure's
    keep_running must stay False anyway, or run_forever's daemon FeedWatcher threads (which loop on
    it) keep running beside the next tenure's set."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "askeve-reviewer"))
    monkeypatch.setattr(ks, "DEFAULT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ks, "_YIELD_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(sync, "sync_enabled", lambda: True)
    runner = _runner()
    root_lease = _lease(root, is_root=True, clock=Clock(), name="sync")
    tenures = []

    def fake_run_forever(keep_running, interval=None, sleep=None):
        tenures.append(keep_running)
        assert keep_running()
        assert not root_lease.try_acquire()  # root gateway asks
        assert not keep_running()
    monkeypatch.setattr(ksc, "run_forever", fake_run_forever)
    after_release = []

    async def between(interval):
        # Root gateway takes the lock, then goes away: the profile's lease is no longer asked to yield.
        assert root_lease.try_acquire()
        root_lease.release()
        after_release.append(tenures[0]())
        runner._running = False
    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    asyncio.run(runner._kanban_sync_watcher())
    assert after_release == [False]


# --- entry path 4: joint restart order in `hermes update` (posix pause resume) --------------------


def _unit(name, home, pid):
    return {"kind": "systemd", "scope": "user", "unit": name, "pid": pid, "home": str(home)}


def test_update_resume_starts_root_first_and_waits_for_its_locks(root, monkeypatch):
    """A pinned profile gateway (older core) never yields, so resume must start the root gateway, wait
    until the root's new pid holds the host locks, and only then start the profile gateways."""
    from hermes_cli import update_cmd_posix_pause as pause
    (root / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
    order = []
    leases = {}

    def start(entry):
        order.append(("start", entry["unit"]))
        if entry["unit"] == "hermes-gateway.service":
            # The root gateway's watchers take both locks a moment later (simulated by this process).
            for name in ("dispatcher", "sync"):
                leases[name] = _lease(root, is_root=True, clock=Clock(), name=name)
        return os.getpid() if entry["unit"] == "hermes-gateway.service" else 99999

    import os
    monkeypatch.setattr(pause, "_start_unit", start)
    waits = []

    def fake_sleep(_s):
        waits.append(len(order))
        for lease in leases.values():
            lease.try_acquire()
    monkeypatch.setattr(ks.time, "sleep", fake_sleep)
    monkeypatch.setattr(pause, "_RESTARTED", {"pids": set(), "units": set(), "labels": set()})
    token = {"posix_units": [
        _unit("hermes-gateway-askeve-reviewer.service", root / "profiles" / "askeve-reviewer", 11),
        _unit("hermes-gateway.service", root, 12),
        _unit("hermes-gateway-askeve-builder.service", root / "profiles" / "askeve-builder", 13),
    ]}
    try:
        pause.resume_paused_set(token)
    finally:
        for lease in leases.values():
            lease.release()
    assert [u for _k, u in order] == ["hermes-gateway.service", "hermes-gateway-askeve-reviewer.service",
                                      "hermes-gateway-askeve-builder.service"]
    assert waits and all(n == 1 for n in waits), "profile units started before the root held its locks"


def test_update_resume_root_lock_wait_is_bounded(root):
    """A lock another process keeps (an older core never yields) costs at most the timeout, then resume goes on."""
    from hermes_cli import update_cmd_posix_pause as pause
    clock = Clock()
    path = root / "kanban" / ".dispatcher.lock"
    other, state = _acquire_singleton_lock(path)
    assert state == "held"

    def sleep(_s):
        clock.now += 30
    try:
        # pid 1 is alive and is not the holder (this test process is).
        assert ks.wait_for_root_locks(1, timeout=90.0, sleep=sleep, clock=clock, locks=[path]) == [".dispatcher.lock"]
        assert 90 <= clock.now - 1000.0 <= 120
    finally:
        _release_singleton_lock(other)
    lone = [_unit("hermes-gateway-x.service", root / "profiles" / "x", 1)]
    assert pause._root_first(lone) == (lone, None)


def test_root_gateway_retries_a_profile_held_lock_every_few_seconds_after_posting_its_claim(root, monkeypatch):
    """hermes-hub 2026-11-12: on all 7 core-update restarts the root gateway found askeve-reviewer/-builder holding
    .sync.lock, posted its claim and then slept the full 30 s retry, although the profile yields within a few seconds
    (should_yield cache 5 s); each restart cost 30-31 s of contention without hub CPU reports. While the root's claim
    is posted it retries every ROOT_CLAIM_RETRY_SECONDS; a profile gateway keeps the slow retry."""
    clock = Clock()
    profile = _lease(root, is_root=False, clock=clock, name="sync", grace=0.0)
    rootl = _lease(root, is_root=True, clock=clock, name="sync")
    assert profile.try_acquire()
    assert rootl.retry_interval() == 30.0  # never tried: the configured interval
    assert not rootl.try_acquire() and rootl.state == "contended"
    assert rootl.retry_interval() == ks.ROOT_CLAIM_RETRY_SECONDS <= 2.0
    other = _lease(root, is_root=False, clock=clock, name="sync")
    assert not other.try_acquire()
    assert other.retry_interval() == 30.0
    profile.release()
    assert rootl.try_acquire() and rootl.retry_interval() == 30.0
    rootl.release()


def test_lease_acquire_loop_sleeps_the_leases_retry_interval(root, monkeypatch):
    runner = _runner()
    attempts, sleeps = iter([False, False, True]), []

    class Lease:
        def try_acquire(self):
            return next(attempts)

        def retry_interval(self):
            return 2.0
    runner._running = True

    async def between(interval):
        sleeps.append(interval)
    monkeypatch.setattr(runner, "_sleep_between_ticks", between)
    assert asyncio.run(runner._acquire_kanban_lease(Lease())) is True
    assert sleeps == [2.0, 2.0]


def test_root_claim_fast_retry_backs_off_against_a_holder_that_never_yields(root):
    """PR #1519 review nit: against a holder that never yields (another root gateway, or a wedged profile gateway) the
    root lease retried, and rewrote its root-claim JSON, every 2 s without bound. The fast retry lasts
    ROOT_CLAIM_FAST_RETRY_WINDOW_SECONDS (~30 s) of contention; after that it falls back to retry_seconds."""
    clock = Clock()
    holder = _lease(root, is_root=False, clock=clock, name="sync", grace=0.0)
    assert holder.try_acquire()
    rootl = _lease(root, is_root=True, clock=clock, name="sync")
    start, intervals = clock.now, []
    while clock.now - start < 150.0:
        assert not rootl.try_acquire() and rootl.state == "contended"
        interval = rootl.retry_interval()
        intervals.append((clock.now - start, interval))
        clock.now += interval
    window = ks.ROOT_CLAIM_FAST_RETRY_WINDOW_SECONDS
    assert 20.0 <= window <= 40.0
    fast = [i for t, i in intervals if t < window]
    slow = [i for t, i in intervals if t >= window]
    assert fast and set(fast) == {ks.ROOT_CLAIM_RETRY_SECONDS}
    assert slow and set(slow) == {30.0}  # retry_seconds again; bounded claim rewrites
    assert len(intervals) <= window / ks.ROOT_CLAIM_RETRY_SECONDS + 1 + 120.0 / 30.0 + 1
    holder.release()
    assert rootl.try_acquire() and rootl.retry_interval() == 30.0
    # A later contention episode gets the fast retry again.
    rootl.release()
    holder2 = _lease(root, is_root=False, clock=clock, name="sync", grace=0.0)
    assert holder2.try_acquire()
    assert not rootl.try_acquire() and rootl.retry_interval() == ks.ROOT_CLAIM_RETRY_SECONDS
    holder2.release()
