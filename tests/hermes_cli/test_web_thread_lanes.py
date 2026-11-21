"""Thread lanes: each route group has its own thread budget and a queue deadline (web_thread_lanes). [daveey]

Live 2026-11-21: the always-on backend's shared AnyIO pool filled with session/kanban reads waiting on locks
for ~2 h; /api/status queued behind them for up to 632 s and Desktop timed out on every call.
"""
import asyncio
import threading
import time

import anyio
import anyio.to_thread
import pytest

from hermes_cli import web_thread_lanes as lanes

TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _lanes():
    lanes.configure({"control": 2, "sessions": 1, "feed": 1, "probe": 1}, queue_deadline_s=5.0)
    yield
    lanes.configure(dict(lanes.DEFAULT_SIZES), lanes.QUEUE_DEADLINE_S)


def _blocker():
    release = threading.Event()
    started = threading.Event()

    def work(value="done"):
        started.set()
        assert release.wait(10), "test never released the blocker"
        return value
    return work, started, release


async def _until(predicate, timeout=TIMEOUT):
    from tests.waiting import async_wait_until
    await async_wait_until(predicate, timeout=timeout)


@pytest.mark.asyncio
async def test_a_full_lane_makes_the_next_caller_wait_then_runs_it():
    work, started, release = _blocker()
    first = asyncio.create_task(lanes.run_in_lane("sessions", work, "first"))
    await _until(started.is_set)
    ran = []
    second = asyncio.create_task(lanes.run_in_lane("sessions", lambda: ran.append(1) or "second"))
    await _until(lambda: lanes.stats()["sessions"]["waiting"] == 1)
    assert ran == [] and not second.done()  # the lane's only thread is taken
    release.set()
    assert await first == "first" and await second == "second" and ran == [1]
    assert lanes.stats()["sessions"]["served"] == 2


@pytest.mark.asyncio
async def test_a_waiter_past_the_deadline_gets_503_and_never_runs():
    lanes.configure({"control": 1, "sessions": 1, "feed": 1, "probe": 1}, queue_deadline_s=0.2)
    work, started, release = _blocker()
    holder = asyncio.create_task(lanes.run_in_lane("sessions", work))
    await _until(started.is_set)
    ran = []
    with pytest.raises(lanes.LaneBusy) as caught:
        await lanes.run_in_lane("sessions", lambda: ran.append(1))
    assert caught.value.status_code == 503 and caught.value.headers["Retry-After"] == str(lanes.RETRY_AFTER_S)
    assert caught.value.detail["reason"] == "deadline" and caught.value.detail["lane"] == "sessions"
    assert ran == []  # refused before it started: a retry is always safe
    release.set()
    await holder
    s = lanes.stats()["sessions"]
    assert s["rejected"] == 1 and s["in_use"] == 0 and s["waiting"] == 0


@pytest.mark.asyncio
async def test_a_per_call_deadline_is_shorter_than_the_lane_deadline():
    work, started, release = _blocker()
    holder = asyncio.create_task(lanes.run_in_lane("probe", work))
    await _until(started.is_set)
    t0 = time.monotonic()
    with pytest.raises(lanes.LaneBusy):
        await lanes.run_in_lane("probe", lambda: None, deadline=0.1)
    assert time.monotonic() - t0 < 1.0  # not the 5 s lane deadline
    release.set()
    await holder


class _Request:
    def __init__(self):
        self.gone = False

    async def is_disconnected(self):
        return self.gone


@pytest.mark.asyncio
async def test_a_waiter_whose_client_left_is_dropped_without_running(monkeypatch):
    monkeypatch.setattr(lanes, "DISCONNECT_POLL_S", 0.01)
    work, started, release = _blocker()
    holder = asyncio.create_task(lanes.run_in_lane("sessions", work))
    await _until(started.is_set)
    request, ran = _Request(), []
    waiter = asyncio.create_task(lanes.run_in_lane("sessions", lambda: ran.append(1), request=request))
    await _until(lambda: lanes.stats()["sessions"]["waiting"] == 1)
    request.gone = True
    with pytest.raises(lanes.LaneBusy) as caught:
        await asyncio.wait_for(waiter, 2)
    assert caught.value.detail["reason"] == "disconnected" and ran == []
    release.set()
    await holder
    assert lanes.stats()["sessions"]["disconnected"] == 1
    # The refused waiter held no slot: the lane takes a new call at once.
    assert await asyncio.wait_for(lanes.run_in_lane("sessions", lambda: "next"), 2) == "next"


@pytest.mark.asyncio
async def test_one_full_lane_never_delays_another():
    work, started, release = _blocker()
    holders = [asyncio.create_task(lanes.run_in_lane("sessions", work))]
    await _until(started.is_set)
    holders += [asyncio.create_task(lanes.run_in_lane("sessions", work)) for _ in range(5)]  # queued
    t0 = time.monotonic()
    assert await asyncio.wait_for(lanes.run_in_lane("control", lambda: "ok"), 2) == "ok"
    assert time.monotonic() - t0 < 1.0
    release.set()
    await asyncio.gather(*holders)


@pytest.mark.asyncio
async def test_lanes_do_not_use_the_shared_default_pool():
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = 1
    release = threading.Event()
    started = threading.Event()

    def hog():
        started.set()
        release.wait(10)
    hog_task = asyncio.create_task(anyio.to_thread.run_sync(hog))
    try:
        await _until(started.is_set)
        # The shared pool is full; a lane call still runs at once.
        assert await asyncio.wait_for(lanes.run_in_lane("control", lambda: "ran"), 2) == "ran"
    finally:
        release.set()
        await hog_task
        limiter.total_tokens = previous


@pytest.mark.asyncio
async def test_lane_runner_runs_a_coalesced_read_on_the_lane():
    from hermes_cli.web_read_coalescing import coalesced_read
    seen = []
    read = coalesced_read(lambda key: seen.append(lanes.stats()["sessions"]["in_use"]) or key,
                          thread_runner=lanes.lane_runner("sessions"))
    assert await read("x") == "x"
    assert seen == [1]  # counted on the sessions lane while it ran


def test_configure_reads_sizes_and_deadline_from_config(monkeypatch):
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda: {"dashboard": {"thread_lanes": {
        "control": 3, "sessions": 12, "feed": "bad", "probe": 0, "queue_deadline_s": 9}}})
    lanes.configure()
    s = lanes.stats()
    assert s["queue_deadline_s"] == 9.0
    assert (s["control"]["size"], s["sessions"]["size"]) == (3, 12)
    # Invalid values keep the defaults.
    assert s["feed"]["size"] == lanes.DEFAULT_SIZES["feed"] and s["probe"]["size"] == lanes.DEFAULT_SIZES["probe"]


def test_an_unconfigured_process_uses_the_defaults_without_reading_config(monkeypatch):
    import hermes_cli.config as config_mod
    monkeypatch.setattr(lanes, "_deadline_s", None)
    monkeypatch.setattr(config_mod, "load_config", lambda: pytest.fail("config read on the event loop path"))
    assert lanes.queue_deadline_s() == lanes.QUEUE_DEADLINE_S
    assert lanes.stats()["sessions"]["size"] == lanes.DEFAULT_SIZES["sessions"]


@pytest.mark.asyncio
async def test_unknown_lane_is_a_programming_error():
    with pytest.raises(KeyError):
        await lanes.run_in_lane("nope", lambda: None)
