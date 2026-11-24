"""One slow, re-requested method cannot take every RPC pool worker (hub, 2026-11-23: all 8 workers
inside ``projects.tree`` while ``session.resume`` timed out for every chat)."""

from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import time

import pytest

from tui_gateway.rpc_method_lanes import MethodLanes


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def test_method_over_its_cap_waits_without_holding_a_worker_and_keeps_order():
    lanes = MethodLanes(2)
    release = threading.Event()
    started, finished, other = [], [], threading.Event()
    lock = threading.Lock()

    def slow(n):
        def fn():
            with lock:
                started.append(n)
            assert release.wait(10)
        return fn

    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            for n in range(6):
                lanes.run(pool.submit, "slow", slow(n), lambda n=n: finished.append(n))
            _wait_until(lambda: len(started) == 2)
            assert lanes.snapshot() == {"slow": {"running": 2, "waiting": 4}}
            # Two workers stay free: another method runs at once although six slow calls were accepted first.
            lanes.run(pool.submit, "other", other.set, lambda: finished.append("other"))
            assert other.wait(10)
            assert sorted(started) == [0, 1]
            release.set()
            _wait_until(lambda: len(finished) == 7)  # before the pool shuts down: waiting calls need it
        finally:
            release.set()
    assert sorted(started[:2]) == [0, 1] and sorted(started) == [0, 1, 2, 3, 4, 5]
    assert sorted(finished, key=str) == [0, 1, 2, 3, 4, 5, "other"]
    assert lanes.snapshot() == {}


def test_no_cap_starts_every_call_at_once():
    lanes = MethodLanes(0)
    assert lanes.cap is None
    release, started = threading.Event(), []
    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            for n in range(3):
                lanes.run(pool.submit, "slow", lambda n=n: (started.append(n), release.wait(10)), lambda: None)
            _wait_until(lambda: len(started) == 3)
        finally:
            release.set()
    assert lanes.snapshot() == {}


def test_first_handoff_failure_propagates_without_the_done_hook_and_frees_the_slot():
    lanes = MethodLanes(1)
    done = []

    def refuse(fn):
        raise RuntimeError("cannot schedule new futures after shutdown")

    with pytest.raises(RuntimeError):
        lanes.run(refuse, "slow", lambda: None, lambda: done.append("first"))
    assert done == [] and lanes.snapshot() == {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        lanes.run(pool.submit, "slow", lambda: None, lambda: done.append("second"))
    assert done == ["second"] and lanes.snapshot() == {}


def test_waiting_call_that_can_no_longer_start_still_gets_its_done_hook():
    """A pool shut down while calls wait: each waiting caller's cleanup runs, none is stranded."""
    lanes = MethodLanes(1)
    release, entered, done = threading.Event(), threading.Event(), []
    live = [True]
    pool = ThreadPoolExecutor(max_workers=1)

    def submit(fn):
        if not live[0]:
            raise RuntimeError("cannot schedule new futures after shutdown")
        return pool.submit(fn)

    try:
        lanes.run(submit, "slow", lambda: (entered.set(), release.wait(10)), lambda: done.append(0))
        assert entered.wait(10)
        lanes.run(submit, "slow", lambda: done.append("ran"), lambda: done.append(1))
        lanes.run(submit, "slow", lambda: done.append("ran"), lambda: done.append(2))
        live[0] = False
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert done == [0, 1, 2]
    assert lanes.snapshot() == {}


def test_cancelled_queued_future_releases_its_slot_to_the_next_waiting_call():
    lanes = MethodLanes(1)
    release, done, ran = threading.Event(), [], threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []

        def submit(fn):
            futures.append(pool.submit(fn))
            return futures[-1]

        blocker = pool.submit(release.wait, 10)  # the only worker is busy: the next submit stays queued
        try:
            lanes.run(submit, "slow", lambda: done.append("ran-0"), lambda: done.append(0))
            lanes.run(submit, "slow", ran.set, lambda: done.append(1))
            assert futures[0].cancel()
            assert done[0] == 0  # cancelled before it started: its cleanup ran on the cancelling thread
            _wait_until(lambda: len(futures) == 2)  # the waiting call is handed over off that thread
        finally:
            release.set()
        blocker.result(10)
        assert ran.wait(10)
    assert done == [0, 1] and lanes.snapshot() == {}


def test_calls_of_one_method_start_in_the_order_they_were_accepted():
    lanes = MethodLanes(1)
    started, finished = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        gate = threading.Event()
        lanes.run(pool.submit, "slow", lambda: (started.append(0), gate.wait(10)), lambda: finished.append(0))
        for n in range(1, 6):
            lanes.run(pool.submit, "slow", lambda n=n: started.append(n), lambda n=n: finished.append(n))
        _wait_until(lambda: started == [0])
        gate.set()
        _wait_until(lambda: len(finished) == 6)
    assert started == [0, 1, 2, 3, 4, 5] and finished == [0, 1, 2, 3, 4, 5]


def test_shutdown_that_cancels_queued_calls_does_not_deadlock_with_waiters():
    """``shutdown(cancel_futures=True)`` cancels under the executor's lock; the hand-off must not
    call ``submit`` on that thread. Every accepted call still gets its done hook."""
    lanes = MethodLanes(1)
    release, done = threading.Event(), []
    pool = ThreadPoolExecutor(max_workers=1)
    blocker = pool.submit(release.wait, 10)  # the only worker is busy: the admitted call stays queued
    try:
        lanes.run(pool.submit, "slow", lambda: done.append("ran"), lambda: done.append(0))
        lanes.run(pool.submit, "slow", lambda: done.append("ran"), lambda: done.append(1))
        lanes.run(pool.submit, "slow", lambda: done.append("ran"), lambda: done.append(2))
        stopper = threading.Thread(target=lambda: pool.shutdown(wait=False, cancel_futures=True), daemon=True)
        stopper.start()
        stopper.join(10)
        assert not stopper.is_alive(), "shutdown deadlocked on the lane hand-off"
        _wait_until(lambda: sorted(done) == [0, 1, 2])
    finally:
        release.set()
    blocker.result(10)
    _wait_until(lambda: lanes.snapshot() == {})
    assert sorted(done) == [0, 1, 2]


class Transport:
    def __init__(self):
        self.frames = queue.Queue()

    def write(self, frame):
        self.frames.put(frame)
        return True


def test_dispatch_keeps_workers_free_for_other_methods(monkeypatch):
    """Entry path: ``server.dispatch``. Eight accepted calls of a slow method use half of a 4-worker
    pool; a later ``session.resume``-like call is answered while all of them are still unanswered."""
    from hermes_cli import backend_retirement
    from tui_gateway import server

    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    release, entered = threading.Event(), threading.Semaphore(0)
    transport = Transport()

    def slow(rid, params):
        entered.release()
        assert release.wait(10)
        return server._ok(rid, {"slow": True})

    monkeypatch.setitem(server._methods, "test.slow", slow)
    monkeypatch.setitem(server._methods, "test.resume", lambda rid, params: server._ok(rid, {"resumed": True}))
    monkeypatch.setattr(server, "_LONG_HANDLERS", {"test.slow", "test.resume"})
    monkeypatch.setattr(server, "_method_lanes", MethodLanes(2))
    with ThreadPoolExecutor(max_workers=4) as pool:
        monkeypatch.setattr(server, "_pool", pool)
        try:
            for n in range(8):
                assert server.dispatch({"id": f"slow-{n}", "method": "test.slow"}, transport) is None
            assert entered.acquire(timeout=10) and entered.acquire(timeout=10)
            assert server.dispatch({"id": "resume", "method": "test.resume"}, transport) is None
            first = transport.frames.get(timeout=10)
            assert first["id"] == "resume" and first["result"] == {"resumed": True}
            # The resume frame is written before its lane entry is cleared (done callback): wait for that.
            _wait_until(lambda: server._method_lanes.snapshot() == {"test.slow": {"running": 2, "waiting": 6}})
        finally:
            release.set()
        answered = sorted(transport.frames.get(timeout=10)["id"] for _ in range(8))
    assert answered == sorted(f"slow-{n}" for n in range(8))
    assert server._method_lanes.snapshot() == {}
    assert backend_retirement.retirement.prepare().get("token")  # every accepted call released its admission


def test_default_cap_is_half_the_pool_and_always_leaves_a_worker():
    from tui_gateway import server

    assert server._method_lanes.cap == min(server._rpc_pool_workers // 2, server._rpc_pool_workers - 1)
    assert 1 <= server._method_lanes.cap < server._rpc_pool_workers
