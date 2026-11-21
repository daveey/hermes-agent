"""/api/status keeps answering while slow reads fill every other thread (web_thread_lanes). [daveey]

Live 2026-11-21 on the hub's always-on backend: ~13.5k Desktop requests in 2 h, slow session/kanban reads
holding the shared AnyIO pool for minutes (session-panel /changes up to 926 s, /api/sessions 627 s), and
/api/status queued behind them for up to 632 s, so Desktop timed out (30 s) on everything. This drives the
real routes into that state and requires /api/status and /api/health to stay fast.
"""
import asyncio
import threading
import time

import anyio.to_thread
import httpx
import pytest
from fastapi import FastAPI

from hermes_cli import web_thread_lanes as lanes

TIMEOUT = 10.0

SHARED_POOL = 4      # the shared AnyIO pool, shrunk so a test can fill it
BURST = 30           # per slow route: well past every pool and lane


async def _until(predicate, timeout=TIMEOUT):
    from tests.waiting import async_wait_until
    await async_wait_until(predicate, timeout=timeout)


@pytest.mark.asyncio
async def test_status_and_health_answer_while_slow_reads_fill_the_shared_pool_and_the_sessions_lane(monkeypatch):
    from hermes_cli.web_routers import sessions as sessions_router
    from hermes_cli.web_routers import status as status_router

    lanes.configure(dict(lanes.DEFAULT_SIZES), lanes.QUEUE_DEADLINE_S)
    release = threading.Event()
    in_kanban = []
    in_other = []

    def slow_kanban_tasks(wanted, stale_ok=False):  # a board read stuck on its lock
        in_kanban.append(1)
        assert release.wait(60)
        return []

    monkeypatch.setattr(sessions_router, "session_kanban_tasks", slow_kanban_tasks)

    app = FastAPI()
    app.include_router(sessions_router.list_router)
    app.include_router(status_router.router)

    @app.get("/slow-other")
    def slow_other():  # any other sync route on the shared pool (the plugins' reads before they move)
        in_other.append(1)
        assert release.wait(60)
        return {"ok": True}

    # Unloaded baseline for the real /api/status in this environment.
    t0 = time.monotonic()
    await asyncio.wait_for(status_router.get_status(), 30)
    baseline = time.monotonic() - t0

    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = SHARED_POOL
    tasks = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     timeout=120) as client:
            tasks += [asyncio.create_task(client.get("/slow-other")) for _ in range(BURST)]
            tasks += [asyncio.create_task(client.get("/api/session-kanban-tasks", params={"ids": f"s{i}"}))
                      for i in range(BURST)]
            # The shared pool is full of stuck reads (with or without lanes), and more are queued.
            await _until(lambda: len(in_other) + len(in_kanban) >= SHARED_POOL
                         and limiter.statistics().tasks_waiting > 0)

            t0 = time.monotonic()
            try:
                status = await asyncio.wait_for(status_router.get_status(), baseline + 3.0)
            except TimeoutError:
                pytest.fail("/api/status is stuck behind the slow reads (the 2026-11-21 outage)")
            took = time.monotonic() - t0
            assert status["version"]
            assert took < baseline + 1.0, f"/api/status took {took:.2f}s under load (unloaded {baseline:.2f}s)"

            health = await asyncio.wait_for(client.get("/api/health"), 2.0)
            assert health.status_code == 200
            report = health.json()["thread_lanes"]
            assert len(in_other) == SHARED_POOL  # the other routes kept the shared pool full throughout
            assert report["sessions"]["in_use"] == lanes.DEFAULT_SIZES["sessions"]
            assert report["sessions"]["waiting"] > 0
            # The slow reads never touched the control lane's budget.
            assert report["control"]["in_use"] == 0 and report["control"]["rejected"] == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        limiter.total_tokens = previous


@pytest.mark.asyncio
async def test_status_does_not_wait_out_a_full_probe_lane(monkeypatch):
    from hermes_cli.web_routers import status as status_router

    lanes.configure(dict(lanes.DEFAULT_SIZES), lanes.QUEUE_DEADLINE_S)
    t0 = time.monotonic()
    await asyncio.wait_for(status_router.get_status(), 30)
    baseline = time.monotonic() - t0

    release = threading.Event()
    started = []

    def stuck_db_read():  # a state.db read that holds its probe thread
        started.append(1)
        release.wait(60)

    holders = [asyncio.create_task(lanes.run_in_lane("probe", stuck_db_read))
               for _ in range(lanes.DEFAULT_SIZES["probe"])]
    try:
        await _until(lambda: len(started) == lanes.DEFAULT_SIZES["probe"])
        t0 = time.monotonic()
        status = await asyncio.wait_for(status_router.get_status(), baseline + 2 * status_router._STATUS_PROBE_DEADLINE_S + 3)
        took = time.monotonic() - t0
        # Each probe-lane read gives up after its short deadline and the field degrades; never the 25 s one.
        assert took < baseline + 2 * status_router._STATUS_PROBE_DEADLINE_S + 1.0
        assert status["version"]
    finally:
        release.set()
        await asyncio.gather(*holders)
