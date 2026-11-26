"""Task-drawer polls must not multiply an already-running SQLite history read."""
import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.web_read_stages import ReadTrace


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="drawer", assignee="worker")
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_detail_flight_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, task_id


def test_overlapping_http_polls_read_history_once(api, monkeypatch):
    module, task_id = api
    entered, release, ready = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    reads, joins = [], []
    original = module._task_detail
    original_join = ReadTrace.join

    def detail(conn, task, *args):
        with lock:
            reads.append(task.id)
            if len(reads) == 4:
                ready.set()
        entered.set()
        assert release.wait(10), "test did not release the owned history reader"
        return original(conn, task, *args)

    def join(trace):
        original_join(trace)
        joins.append(trace)
        if len(joins) == 4:
            ready.set()

    monkeypatch.setattr(module, "_task_detail", detail)
    monkeypatch.setattr(ReadTrace, "join", join)

    async def exercise():
        app = FastAPI()
        app.include_router(module.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            calls = [asyncio.create_task(client.get(f"/tasks/{task_id}"))]
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                calls.extend(asyncio.create_task(client.get(f"/tasks/{task_id}")) for _ in range(3))
                assert await asyncio.to_thread(ready.wait, 10), "callers reached neither the flight nor SQLite"
                assert reads == [task_id], "overlapping polls started redundant SQLite history reads"
            finally:
                release.set()
                responses = await asyncio.gather(*calls)
            assert all(r.status_code == 200 for r in responses)
            assert all(r.json() == responses[0].json() for r in responses)
            assert responses[0].json()["task"]["id"] == task_id
            fresh = await client.get(f"/tasks/{task_id}")
            assert fresh.status_code == 200
            assert reads == [task_id, task_id], "completed results must not be cached"

    asyncio.run(exercise())


def test_cancelled_http_waiter_retains_running_read(api, monkeypatch):
    module, task_id = api
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    reads, joins = [], []
    original, original_join = module._task_detail, ReadTrace.join

    def detail(conn, task, *args):
        reads.append(task.id)
        entered.set()
        assert release.wait(10)
        return original(conn, task, *args)

    def join(trace):
        original_join(trace)
        joins.append(trace)
        if len(joins) == 2:
            joined.set()

    monkeypatch.setattr(module, "_task_detail", detail)
    monkeypatch.setattr(ReadTrace, "join", join)

    async def exercise():
        app = FastAPI()
        app.include_router(module.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            leader = asyncio.create_task(client.get(f"/tasks/{task_id}"))
            follower = None
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                leader.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await leader
                follower = asyncio.create_task(client.get(f"/tasks/{task_id}"))
                assert await asyncio.to_thread(joined.wait, 10)
                assert reads == [task_id]
            finally:
                release.set()
                if follower is not None:
                    response = await follower
                else:
                    await asyncio.gather(leader, return_exceptions=True)
            assert response.status_code == 200
            assert response.json()["task"]["id"] == task_id
            with kbc.connect_closing() as conn:
                conn.execute("UPDATE tasks SET title=? WHERE id=?", ("edited after cancellation", task_id))
                conn.commit()
            fresh = await client.get(f"/tasks/{task_id}")
            assert fresh.json()["task"]["title"] == "edited after cancellation"
            assert reads == [task_id, task_id]

    asyncio.run(exercise())


def test_slow_task_does_not_queue_other_tasks_or_run_filters(api, monkeypatch):
    module, task_id = api
    with kbc.connect_closing() as conn:
        others = [kb.create_task(conn, title=f"other {i}", assignee="worker") for i in range(5)]
    entered, release = threading.Event(), threading.Event()
    blocked = {task_id, *others[:3]}
    readers, lock = [], threading.Lock()
    original = module._task_detail

    def detail(conn, task, state_type, state_name):
        if task.id in blocked and state_type is None:
            with lock:
                readers.append(task.id)
                if len(readers) == 4:
                    entered.set()
            assert release.wait(20)
        return original(conn, task, state_type, state_name)

    monkeypatch.setattr(module, "_task_detail", detail)

    async def exercise():
        app = FastAPI()
        app.include_router(module.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            slow = [asyncio.create_task(client.get(f"/tasks/{tid}")) for tid in [task_id, *others[:3]]]
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                urls = [f"/tasks/{tid}" for tid in others[3:]]
                urls.append(f"/tasks/{task_id}?run_state_type=status&run_state_name=running")
                responses = await asyncio.wait_for(asyncio.gather(*(client.get(url) for url in urls)), 10)
                assert all(r.status_code == 200 for r in responses)
                assert [r.json()["task"]["id"] for r in responses] == [*others[3:], task_id]
                assert all(not call.done() for call in slow), "unrelated reads completed only after slow reads"
            finally:
                release.set()
                await asyncio.gather(*slow)

    asyncio.run(exercise())


@pytest.mark.parametrize("explicit", [False, True])
def test_board_switch_does_not_join_old_board(api, monkeypatch, explicit):
    module, task_id = api
    kb.create_board("other")
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        columns, values = list(row.keys()), list(row)
        values[columns.index("title")] = "other board"
    with kbc.connect_closing(board="other") as conn:
        conn.execute(f"INSERT INTO tasks ({','.join(columns)}) VALUES ({','.join('?' for _ in values)})", values)
        conn.commit()
    entered, release = threading.Event(), threading.Event()
    original = module._task_detail

    def detail(conn, task, *args):
        if task.title == "drawer":
            entered.set()
            assert release.wait(10)
        return original(conn, task, *args)

    monkeypatch.setattr(module, "_task_detail", detail)

    async def exercise():
        app = FastAPI()
        app.include_router(module.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            leader = asyncio.create_task(client.get(f"/tasks/{task_id}"))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                if not explicit:
                    kb.set_current_board("other")
                query = "?board=other" if explicit else ""
                other = await asyncio.wait_for(client.get(f"/tasks/{task_id}{query}"), 10)
                assert other.status_code == 200
                assert other.json()["task"]["title"] == "other board"
                assert not leader.done()
            finally:
                release.set()
                first = await leader
                kb.set_current_board("default")
            assert first.json()["task"]["title"] == "drawer"

    asyncio.run(exercise())


@pytest.mark.parametrize("query, code", [("?run_state_type=status", 400),
                                       ("?run_state_type=other&run_state_name=x", 400), ("", 404)])
def test_errors_remain_http_errors_and_are_not_retained(api, query, code):
    module, _task_id = api

    async def exercise():
        app = FastAPI()
        app.include_router(module.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            for _ in range(2):
                response = await client.get(f"/tasks/missing{query}")
                assert response.status_code == code
                assert "detail" in response.json()

    asyncio.run(exercise())
