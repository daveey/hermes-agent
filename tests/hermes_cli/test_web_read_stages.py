"""Passive coalescer stages distinguish queueing from an executing worker."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from itertools import pairwise

import httpx
import pytest
from anyio.to_thread import run_sync
from fastapi import FastAPI

from hermes_cli.web_read_coalescing import coalesced_read
from hermes_cli import web_request_stages as stages


class Sink:
    def __init__(self):
        self.rows = []

    def emit(self, stage, **fields):
        self.rows.append({"stage": stage, "mono_ns": time.monotonic_ns(), **fields})


@pytest.mark.asyncio
async def test_coalesced_worker_admission_and_cancelled_leader_are_distinct():
    sink = Sink()
    entered = {key: threading.Event() for key in "abc"}
    release = threading.Event()
    calls = []

    def read(board):
        calls.append(board)
        entered[board].set()
        assert release.wait(5)
        return {"board": board}

    wrapped = coalesced_read(read, admission_key=lambda a: a["board"], max_concurrent=2)
    app = FastAPI()

    @app.get("/api/plugins/kanban/board")
    async def board(board: str):
        return await wrapped(board)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    observed = stages.RequestStageIngress(stages.RequestStageHandler(app), sink)
    tasks = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url="http://test") as client:
        async def request(key, rid):
            return await client.get("/api/plugins/kanban/board", params={"board": key},
                                    headers={"x-hermes-req-id": rid * 32})

        try:
            tasks.append(asyncio.create_task(request("a", "a")))
            assert await run_sync(entered["a"].wait, 2)
            tasks.append(asyncio.create_task(request("b", "b")))
            assert await run_sync(entered["b"].wait, 2)
            tasks.append(asyncio.create_task(request("c", "c")))
            tasks.append(asyncio.create_task(request("a", "d")))
            # A healthy request proves the loop progresses while both workers wait.
            assert (await client.get("/api/health")).json() == {"ok": True}
            await asyncio.sleep(0)
            read_rows = [r for r in sink.rows if r["stage"].startswith("read_")]
            assert len([r for r in read_rows if r["stage"] == "read_join"]) == 4
            assert not entered["c"].is_set()
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert not any(r["stage"] == "read_worker_end" for r in read_rows)
            release.set()
            replies = await asyncio.wait_for(asyncio.gather(*tasks[1:]), 3)
            assert [r.json()["board"] for r in replies] == ["b", "c", "a"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert sorted(calls) == list("abc")
    joins = [r for r in sink.rows if r["stage"] == "read_join"]
    assert joins[0]["read_id"] == joins[3]["read_id"]
    assert len({r["read_id"] for r in joins}) == 3
    for join in joins[:3]:
        rows = [r for r in sink.rows if r.get("read_id") == join["read_id"]
                and r["stage"] != "read_join"]
        assert [r["stage"] for r in rows] == [
            "read_group_wait", "read_admission_wait", "read_runner_submit",
            "read_worker_start", "read_worker_end", "read_result",
        ]
        assert all(a["mono_ns"] <= b["mono_ns"] for a, b in pairwise(rows))
        assert all(r["id"] == join["id"] for r in rows)
    assert "board=" not in json.dumps(sink.rows)


@pytest.mark.asyncio
async def test_real_tcp_separates_anyio_queue_from_worker_execution():
    import socket
    import uvicorn
    from anyio.to_thread import current_default_thread_limiter
    from tests.waiting import async_wait_until

    sink = Sink()
    release = threading.Event()
    occupied = threading.Event()
    limiter = current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = 1

    def occupy():
        occupied.set()
        assert release.wait(10)

    read = coalesced_read(lambda: {"ok": True})
    app = FastAPI()

    @app.get("/api/plugins/kanban/board")
    async def board():
        return await read()

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    observed = stages.RequestStageIngress(stages.RequestStageHandler(app), sink)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(observed, lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    blocker = asyncio.create_task(run_sync(occupy))
    request = None
    try:
        await async_wait_until(lambda: occupied.is_set() and server.started, timeout=4, what="TCP and worker")
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                                     trust_env=False, timeout=4) as client:
            request = asyncio.create_task(client.get("/api/plugins/kanban/board",
                                                     headers={"x-hermes-req-id": "f" * 32}))
            await async_wait_until(lambda: any(r["stage"] == "read_runner_submit" for r in sink.rows),
                                   timeout=3, what="read queued")
            assert (await client.get("/api/health")).json() == {"ok": True}
            assert not any(r["stage"] == "read_worker_start" for r in sink.rows)
            release.set()
            assert (await request).json() == {"ok": True}
            rows = [r for r in sink.rows if r.get("id") == "f" * 32]
            names = [r["stage"] for r in rows]
            assert names.index("read_runner_submit") < names.index("read_worker_start")
            assert names.index("read_worker_end") < names.index("read_result") < names.index("response_start")
    finally:
        release.set()
        await blocker
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
        limiter.total_tokens = previous


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["worker", "runner"])
async def test_failure_stages_preserve_exception_and_reset_context(failure):
    sink = Sink()
    error = ValueError("PRIVATE_EXCEPTION")

    def read():
        raise error

    async def runner(function):
        if failure == "runner":
            raise error
        return await run_sync(function)

    wrapped = coalesced_read(read, thread_runner=runner)

    async def app(scope, receive, send):
        await wrapped()

    observed = stages.RequestStageIngress(stages.RequestStageHandler(app), sink)
    scope = {"type": "http", "method": "GET", "path": "/api/plugins/kanban/board", "headers": []}
    with pytest.raises(ValueError) as caught:
        await observed(scope, None, None)
    assert caught.value is error
    names = [r["stage"] for r in sink.rows]
    assert "read_result" in names
    assert ("read_worker_end" in names) == (failure == "worker")
    assert "PRIVATE_EXCEPTION" not in json.dumps(sink.rows)
    before = list(sink.rows)
    with pytest.raises(ValueError):
        await wrapped()  # outside middleware: no retained request context
    assert sink.rows == before


@pytest.mark.asyncio
async def test_traced_overflow_preserves_runner_admission():
    from tests.waiting import async_wait_until

    sink = Sink()
    release = asyncio.Event()
    admitted = []

    async def runner(function):
        admitted.append(function)
        await release.wait()
        return function()

    wrapped = coalesced_read(lambda key: key, thread_runner=runner, overflow_to_runner=True)
    results = []

    async def app(scope, receive, send):
        results.append(await wrapped(scope["key"]))

    observed = stages.RequestStageIngress(stages.RequestStageHandler(app), sink)
    tasks = [asyncio.create_task(observed({"type": "http", "method": "GET",
              "path": "/api/sessions", "headers": [(b"x-hermes-req-id", key.encode() * 32)],
              "key": key}, None, None)) for key in "ab"]
    try:
        await async_wait_until(lambda: len(admitted) == 2, timeout=3, what="overflow runner admission")
        second = [r["stage"] for r in sink.rows if r.get("id") == "b" * 32]
        assert "read_runner_submit" in second
        assert "read_admission_wait" not in second
        release.set()
        await asyncio.gather(*tasks)
        assert sorted(results) == ["a", "b"]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len({r["read_id"] for r in sink.rows if r["stage"] == "read_join"}) == 2


