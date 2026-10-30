"""HTTP admission regression: board bursts must not consume every worker."""
import asyncio
import importlib.util
import os
import sys
import threading
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from hermes_cli import kanban_db as kb

OK = 200
BOARD_PATH = "/api/plugins/kanban/board"
STATUS_PATH = "/probe"
BURST_SIZE = 8


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_board_burst_preserves_http_worker_capacity(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    plugin_path = Path(os.environ.get(
        "HERMES_TEST_KANBAN_PLUGIN",
        str(Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"),
    ))
    spec = importlib.util.spec_from_file_location("kanban_admission_test", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")

    @app.get(STATUS_PATH)
    async def probe():
        return await run_in_threadpool(lambda: {"ready": True})

    entered = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    calls = 0
    original = kb.list_tasks

    def blocked_read(*args, **kwargs):
        nonlocal calls
        with count_lock:
            calls += 1
        entered.set()
        assert release.wait(10), "test did not release board read"
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "list_tasks", blocked_read)
    limiter = anyio.to_thread.current_default_thread_limiter()
    old_tokens = limiter.total_tokens
    limiter.total_tokens = 2
    requests = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            requests = [asyncio.create_task(client.get(BOARD_PATH)) for _ in range(BURST_SIZE)]
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            response = await asyncio.wait_for(client.get(STATUS_PATH), timeout=2)
            assert response.status_code == OK
            release.set()
            responses = await asyncio.gather(*requests)
            assert all(r.status_code == OK for r in responses)
            assert calls == 1
    finally:
        release.set()
        if requests:
            await asyncio.gather(*requests, return_exceptions=True)
        limiter.total_tokens = old_tokens

@pytest.mark.anyio
async def test_cold_board_read_does_not_queue_other_boards(tmp_path, monkeypatch):
    """Mac 16:30 cold start: a slow read of one board held the only board slot,
    so reads of every other board waited behind it (6-20 s, Desktop timeout 6 s)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    kb.create_board("other")
    plugin_path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_admission_multi_test", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")

    entered = threading.Event()
    release = threading.Event()
    first = threading.Lock()
    original = kb.list_tasks

    def first_read_blocks(*args, **kwargs):
        if first.acquire(blocking=False):  # only the first (default board) read stalls
            entered.set()
            assert release.wait(10), "test did not release board read"
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "list_tasks", first_read_blocks)
    slow = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            slow = asyncio.create_task(client.get(BOARD_PATH, params={"board": "default"}))
            for _ in range(300):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            other = await asyncio.wait_for(client.get(BOARD_PATH, params={"board": "other"}), timeout=3)
            assert other.status_code == OK
            assert not slow.done()
            release.set()
            assert (await slow).status_code == OK
    finally:
        release.set()
        if slow is not None:
            await asyncio.gather(slow, return_exceptions=True)


@pytest.mark.anyio
async def test_board_reads_hold_at_most_two_workers(tmp_path, monkeypatch):
    """Per-board admission is still bounded: with three cold boards, the third
    waits until one of the first two finishes. Hub A/B: a cap of 4 made the
    default board's own cold read hit the Desktop's 6 s timeout (2/5 runs)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    kb.create_board("other")
    kb.create_board("third")
    plugin_path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_admission_cap_test", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")

    release = threading.Event()
    guard = threading.Lock()
    state = {"active": 0, "peak": 0, "entered": 0}
    original = kb.list_tasks

    def blocking_read(*args, **kwargs):
        with guard:
            state["active"] += 1
            state["entered"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            assert release.wait(10), "test did not release board reads"
            return original(*args, **kwargs)
        finally:
            with guard:
                state["active"] -= 1

    monkeypatch.setattr(kb, "list_tasks", blocking_read)
    reads = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            reads = [asyncio.create_task(client.get(BOARD_PATH, params={"board": b}))
                     for b in ("default", "other", "third")]
            for _ in range(300):
                if state["entered"] >= 2:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)  # health: allow HX013 -- negative check: window for a wrongly admitted third read to start
            assert state["entered"] == 2
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*reads), timeout=10)
            assert all(r.status_code == OK for r in responses)
            assert state["peak"] == 2
            assert state["entered"] == 3
    finally:
        release.set()
        if reads:
            await asyncio.gather(*reads, return_exceptions=True)
