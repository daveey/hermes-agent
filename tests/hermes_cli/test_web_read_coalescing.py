"""GET /api/profiles coalesces a poll burst before AnyIO threadpool admission (#105179)."""
import asyncio
import threading
from functools import wraps

import pytest


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


@async_test
async def test_profiles_burst_leaves_threadpool_status_responsive(monkeypatch):
    import anyio.to_thread
    import httpx
    from fastapi import FastAPI
    from hermes_cli import profiles as profiles_mod
    from hermes_cli.web_routers import profiles
    from starlette.concurrency import run_in_threadpool
    release = threading.Event()
    calls = []

    def slow_profiles(**_kwargs):  # the router passes ``lazy_skill_count=True``
        calls.append(1)
        assert release.wait(3)
        return ["example"]

    monkeypatch.setattr(profiles_mod, "list_profiles", slow_profiles)
    monkeypatch.setattr(profiles, "_profile_to_dict", lambda p: {"name": p})
    monkeypatch.setattr(profiles, "run_in_threadpool", run_in_threadpool)
    app = FastAPI()
    app.include_router(profiles.router)

    @app.get("/api/status")
    def status():
        return {"ok": True}

    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = 2
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            tasks = [asyncio.create_task(client.get("/api/profiles")) for _ in range(12)]
            try:
                await wait_until(lambda: calls)
                await asyncio.sleep(0.05)
                response = await asyncio.wait_for(client.get("/api/status"), 0.5)
                assert response.status_code == 200
                assert response.json() == {"ok": True}
                assert calls == [1]
                assert limiter.borrowed_tokens == 1
            finally:
                release.set()
                responses = await asyncio.gather(*tasks)
            assert all(r.status_code == 200 for r in responses)
            assert all(r.json() == {"profiles": [{"name": "example"}]} for r in responses)
    finally:
        limiter.total_tokens = previous


def _gated_runner(log, gates):
    async def runner(call):
        name = call.args[0]
        log.append(("start", name))
        await gates[name].wait()
        log.append(("end", name))
        return call()
    return runner


@async_test
async def test_admission_groups_run_concurrently_up_to_cap():
    from hermes_cli.web_read_coalescing import coalesced_read
    log = []
    gates = {n: asyncio.Event() for n in "abcd"}
    read = coalesced_read(lambda board, n=0: (board, n), thread_runner=_gated_runner(log, gates),
                          admission_key=lambda a: a["board"], max_concurrent=2)
    a1 = asyncio.create_task(read("a"))
    b1 = asyncio.create_task(read("b"))
    c1 = asyncio.create_task(read("c"))
    a2 = asyncio.create_task(read("a", 1))  # same group, different args: waits for a1
    a1_dup = asyncio.create_task(read("a"))  # identical args: coalesces with a1
    await wait_until(lambda: len(log) == 2)
    for _ in range(50):
        await asyncio.sleep(0)  # let queued tasks run; none may be admitted
    assert sorted(log) == [("start", "a"), ("start", "b")]  # cap 2: c queued, a2 behind a1
    gates["b"].set()
    await wait_until(lambda: ("start", "c") in log)
    assert log.count(("start", "a")) == 1
    gates["c"].set()
    gates["a"].set()
    assert await a1 == ("a", 0) and await a1_dup == ("a", 0)
    assert await b1 == ("b", 0) and await c1 == ("c", 0) and await a2 == ("a", 1)
    assert log.count(("start", "a")) == 2


@async_test
async def test_slow_group_does_not_block_other_group():
    from hermes_cli.web_read_coalescing import coalesced_read
    gates = {n: asyncio.Event() for n in "ab"}
    gates["b"].set()
    read = coalesced_read(lambda board: board, thread_runner=_gated_runner([], gates),
                          admission_key=lambda a: a["board"], max_concurrent=4)
    slow = asyncio.create_task(read("a"))
    for _ in range(50):
        await asyncio.sleep(0)
    assert await asyncio.wait_for(read("b"), 1) == "b"
    assert not slow.done()
    gates["a"].set()
    assert await slow == "a"


@async_test
async def test_default_admission_still_serializes_distinct_args():
    from hermes_cli.web_read_coalescing import coalesced_read
    log = []
    gates = {n: asyncio.Event() for n in "ab"}
    read = coalesced_read(lambda name: name, thread_runner=_gated_runner(log, gates))
    a = asyncio.create_task(read("a"))
    b = asyncio.create_task(read("b"))
    await wait_until(lambda: log)
    for _ in range(50):
        await asyncio.sleep(0)  # let queued tasks run; none may be admitted
    assert len(log) == 1
    gates["a"].set(); gates["b"].set()
    assert {await a, await b} == {"a", "b"}


def test_max_concurrent_requires_admission_key():
    from hermes_cli.web_read_coalescing import coalesced_read
    with pytest.raises(ValueError):
        coalesced_read(lambda: 1, max_concurrent=2)
    with pytest.raises(ValueError):
        coalesced_read(lambda: 1, admission_key=lambda a: 1, max_concurrent=0)
