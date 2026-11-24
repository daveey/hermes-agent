"""Checkpoint store pruning rides the gateway housekeeping tick, not the constructor.

Its ``git gc`` repacks the whole store (tens of seconds on a GB store); run at construction it
delayed the control socket, adapters and the code_sha stamp, so the first restart of the day (the
``hermes update`` one) looked hung and failed fleet verification.

The tick only *requests* the prune: it runs on its own ``gateway-checkpoint-prune`` thread. Run
inline as the tick's last chore, one real prune held the housekeeping thread for 9 min 20 s: no
runtime heartbeat landed (the gateway read as wedged) and the cron-delivery drain stopped.
"""

import contextvars
import logging
import threading
from types import SimpleNamespace

import gateway.run as gateway_run

PRUNE_THREAD = "gateway-checkpoint-prune"
LOOP_THREAD = "gateway-housekeeping"
SAFETY_RELEASE_S = 10
JOIN_S = 5
HELD = "the housekeeping loop is held by the checkpoint prune"


class _OneTickStopEvent:
    """One tick, no waiting: for chores that run on the loop's own thread (imported by other suites)."""

    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


class _TickStopEvent:
    """Stops the housekeeping loop after ``ticks`` ticks; ``on_tick[n]`` runs in tick n's wait."""

    def __init__(self, ticks=1, on_tick=None):
        self.ticks, self.waits, self.on_tick = ticks, 0, on_tick or {}

    def is_set(self):
        return self.waits >= self.ticks

    def wait(self, timeout=None):
        self.waits += 1
        hook = self.on_tick.get(self.waits)
        if hook is not None:
            hook()
        return self.is_set()


class _Prune:
    """A stand-in ``auto_prune_from_config`` that records where it ran and can be held open."""

    def __init__(self, monkeypatch, *, blocked=False):
        import tools.checkpoint_maintenance as cm

        self.entered, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.threads, self.seen, self.done = [], [], 0
        self.observe = lambda: None
        if not blocked:
            self.release.set()
        monkeypatch.setattr(cm, "auto_prune_from_config", self)

    @property
    def entries(self):
        return len(self.threads)

    def __call__(self):
        self.threads.append(threading.current_thread())
        self.seen.append(self.observe())
        self.entered.set()
        self.release.wait(SAFETY_RELEASE_S)
        self.done += 1
        self.finished.set()
        return {"skipped": False}

    def wait_entered(self):
        assert self.entered.wait(JOIN_S), "the prune was never entered"

    def wait_finished(self):
        assert self.finished.wait(JOIN_S), "the requested prune never ran"


def _run_housekeeping(stop, *, before=None, **kwargs):
    """The loop on a thread named like the real one; joined with a timeout so a held loop fails
    instead of hanging the suite."""
    def target():
        if before is not None:
            before()
        gateway_run._start_gateway_housekeeping(stop, interval=0, **kwargs)

    thread = threading.Thread(target=target, name=LOOP_THREAD, daemon=True)
    thread.start()
    thread.join(JOIN_S)
    return thread


def _settle(prune, loop_thread=None):
    """Release a held prune and join every thread the test started, before its patches unwind."""
    prune.release.set()
    for thread in [loop_thread, *prune.threads]:
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_S)


def _worker(**kwargs):
    import pytest

    try:
        from gateway.run_checkpoint_prune import CheckpointPruneWorker
    except ModuleNotFoundError as exc:
        if exc.name != "gateway.run_checkpoint_prune":
            raise
        # Old gateways have no worker API. Prove the missing behavior, rather than
        # counting an import failure as a regression failure.
        from unittest.mock import patch

        seen = []
        with patch("tools.checkpoint_maintenance.auto_prune_from_config",
                   side_effect=lambda: seen.append(threading.current_thread().name)):
            gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)
        assert seen == [PRUNE_THREAD], "checkpoint pruning still runs inline, without worker lifecycle"
        pytest.fail("asynchronous pruning exists but its request/close API is unavailable")
    return CheckpointPruneWorker(logging.getLogger("test.checkpoint_prune"), **kwargs)


def _stop_worker(worker, prune=None):
    worker.close()
    if prune is not None:
        prune.release.set()
    if worker.thread is not None:
        worker.thread.join(JOIN_S)
        assert not worker.thread.is_alive()


def test_gateway_housekeeping_runs_the_checkpoint_prune(monkeypatch):
    prune = _Prune(monkeypatch)
    # One tick; its wait gives the prune worker a bounded moment to run the requested pass.
    stop = _TickStopEvent(1, {1: prune.wait_finished})
    try:
        gateway_run._start_gateway_housekeeping(stop, interval=0)
    finally:
        _settle(prune)

    assert prune.entries == 1 and prune.done == 1


def test_blocked_prune_does_not_hold_heartbeat_or_cron_delivery_drain(monkeypatch):
    """T1, the defect: three ticks complete, each with its heartbeat and drain, under a held prune."""
    from cron import scheduler

    prune = _Prune(monkeypatch, blocked=True)
    stop = _TickStopEvent(3, {1: prune.wait_entered})
    heartbeat_ticks, drain_ticks = [], []
    monkeypatch.setattr(gateway_run, "_write_runtime_status_quiet",
                        lambda **_fields: heartbeat_ticks.append(stop.waits + 1))
    monkeypatch.setattr(scheduler, "drain_delivery_queue",
                        lambda _adapters, _loop: drain_ticks.append(stop.waits + 1), raising=False)
    loop_thread = _run_housekeeping(stop, adapters={}, loop=object())
    try:
        assert not loop_thread.is_alive(), HELD
        assert prune.entered.is_set() and not prune.release.is_set() and prune.done == 0
        assert heartbeat_ticks == [1, 2, 3]
        assert {1, 2, 3} <= set(drain_ticks)
    finally:
        _settle(prune, loop_thread)


def test_ticks_during_a_prune_are_single_flight_and_coalesce(monkeypatch):
    """T2: three ticks under a held prune enter it once; the requests made meanwhile cost one
    follow-up pass (which the 24 h marker turns into an immediate return in production)."""
    from tests.waiting import wait_until

    prune = _Prune(monkeypatch, blocked=True)
    at_third_tick = []

    def third_tick():
        at_third_tick.append((prune.entries, prune.done))
        prune.release.set()
        wait_until(lambda: prune.done >= 2, timeout=JOIN_S, what="the coalesced follow-up pass")

    stop = _TickStopEvent(3, {1: prune.wait_entered, 3: third_tick})
    loop_thread = _run_housekeeping(stop)
    try:
        assert not loop_thread.is_alive(), HELD
        assert at_third_tick == [(1, 0)]
    finally:
        _settle(prune, loop_thread)
    assert prune.entries == 2 and prune.done == 2


def test_each_prune_pass_holds_one_housekeeping_lease_on_the_prune_thread(monkeypatch):
    """T3: drain custody is per pass, on the prune thread; a refusal runs nothing and kills nothing."""
    from hermes_cli import worker_drain_fence as fence

    prune = _Prune(monkeypatch)
    events, admits = [], []
    refused, closed = threading.Event(), threading.Event()
    behaviours = ["admit", "refuse", "admit"]

    def admit(purpose):
        behaviour = behaviours.pop(0)
        admits.append((purpose, threading.current_thread().name, behaviour))
        if behaviour == "refuse":
            refused.set()
            raise fence.DrainRefused("draining")
        lease = SimpleNamespace(fd=None, closed=False)

        def close():
            assert not lease.closed
            lease.closed = True
            events.append("close")
            closed.set()

        lease.close = close
        events.append("admit")
        return lease

    def observe():
        lease = fence._authority.get()
        events.append("prune")
        return lease is not None and not lease.closed

    monkeypatch.setattr(fence, "admit", admit)
    prune.observe = observe
    worker = _worker()
    try:
        worker.request()
        assert closed.wait(JOIN_S)
        assert events == ["admit", "prune", "close"] and prune.seen == [True]

        worker.request()
        assert refused.wait(JOIN_S)
        closed.clear()
        worker.request()
        assert closed.wait(JOIN_S)
        assert worker.thread.is_alive()
    finally:
        _stop_worker(worker, prune)
    assert events == ["admit", "prune", "close"] * 2, "the refused pass must not prune"
    assert admits == [("gateway.housekeeping", PRUNE_THREAD, b) for b in ("admit", "refuse", "admit")]


def test_a_failing_prune_leaves_the_worker_alive(monkeypatch):
    """T4."""
    import tools.checkpoint_maintenance as cm

    outcomes, ran = [RuntimeError("gc failed"), None], threading.Semaphore(0)

    def flaky():
        outcome = outcomes.pop(0)
        ran.release()
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(cm, "auto_prune_from_config", flaky)
    worker = _worker()
    try:
        worker.request()
        assert ran.acquire(timeout=JOIN_S)
        worker.request()
        assert ran.acquire(timeout=JOIN_S), "the worker died with the failed prune"
        assert worker.thread.is_alive() and outcomes == []
    finally:
        _stop_worker(worker)


def test_the_prune_runs_on_its_own_daemon_thread(monkeypatch):
    """T5."""
    prune = _Prune(monkeypatch)
    stop = _TickStopEvent(1, {1: prune.wait_finished})
    loop_thread = _run_housekeeping(stop)
    try:
        assert not loop_thread.is_alive()
        assert [(t.name, t.daemon) for t in prune.threads] == [(PRUNE_THREAD, True)]
        assert loop_thread not in prune.threads
    finally:
        _settle(prune, loop_thread)


def test_worker_exits_with_the_loop_and_starts_no_prune_after_stop(monkeypatch):
    """T6: requests still pending when the loop stops are dropped, and the thread ends."""
    prune = _Prune(monkeypatch, blocked=True)
    stop = _TickStopEvent(3, {1: prune.wait_entered})
    loop_thread = _run_housekeeping(stop)
    try:
        assert not loop_thread.is_alive(), HELD
        assert [t.name for t in prune.threads] == [PRUNE_THREAD]
        worker_thread = prune.threads[0]
        assert worker_thread.is_alive()  # the running pass is not interrupted

        prune.release.set()
        worker_thread.join(2)
        assert not worker_thread.is_alive()
        assert prune.entries == 1 and prune.done == 1, "a prune started after the loop stopped"
    finally:
        _settle(prune, loop_thread)


def test_no_pass_starts_after_close(monkeypatch):
    """T6, at the worker: a request made after ``close()`` runs nothing and starts no thread; an
    idle worker thread exits within 2 s of it."""
    prune = _Prune(monkeypatch)
    idle = _worker()
    try:
        idle.request()
        prune.wait_finished()
        idle.close()
        idle.thread.join(2)
        assert not idle.thread.is_alive()
        idle.request()
    finally:
        _stop_worker(idle, prune)

    never_started = _worker()
    never_started.close()
    never_started.request()
    assert never_started.thread is None and prune.entries == 1


def test_surviving_prune_retains_runtime_lock_after_loop_cleanup(monkeypatch, tmp_path):
    """The real ownership cleanup must retain custody until the last prune writer exits."""
    from gateway import runtime_ownership
    from gateway.status import is_gateway_runtime_lock_active

    home = tmp_path / "owned-profile"
    home.mkdir()
    owner = runtime_ownership.ProfileOwnership()
    owner.reserve([home])
    monkeypatch.setattr(runtime_ownership, "process_ownership", owner)
    prune = _Prune(monkeypatch, blocked=True)
    stop = _TickStopEvent(1, {1: prune.wait_entered})
    loop_thread = None
    try:
        loop_thread = _run_housekeeping(stop)
        assert not loop_thread.is_alive(), HELD
        assert prune.done == 0 and prune.threads[0].is_alive()
        owner.close()
        assert is_gateway_runtime_lock_active(home / "gateway.lock"), (
            "cleanup released runtime custody while the prune writer was still alive")
    finally:
        _settle(prune, loop_thread)
        owner.close()
    assert not is_gateway_runtime_lock_active(home / "gateway.lock")


def test_the_prune_sees_the_housekeeping_threads_context(monkeypatch, tmp_path):
    """T7: profile and home scope are contextvars; the prune thread resolves the loop thread's."""
    from hermes_constants import get_hermes_home, set_hermes_home_override

    marker = contextvars.ContextVar("checkpoint_prune_test_scope", default="unset")
    scoped_home = tmp_path / "scoped-home"
    scoped_home.mkdir()
    prune = _Prune(monkeypatch)
    prune.observe = lambda: (marker.get(), get_hermes_home())
    stop = _TickStopEvent(1, {1: prune.wait_finished})

    def bind_scope():
        marker.set("housekeeping-scope")
        set_hermes_home_override(scoped_home)

    loop_thread = _run_housekeeping(stop, before=bind_scope)
    try:
        assert not loop_thread.is_alive()
        assert [t.name for t in prune.threads] == [PRUNE_THREAD]
        assert prune.seen == [("housekeeping-scope", scoped_home)]
        assert marker.get() == "unset" and get_hermes_home() != scoped_home
    finally:
        _settle(prune, loop_thread)
