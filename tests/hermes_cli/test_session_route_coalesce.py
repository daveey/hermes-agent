"""Concurrent session-kanban-tasks and session-detail polls share one in-flight read."""
import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from hermes_cli.web_routers import sessions
from tests.waiting import async_wait_until


async def _until(predicate):
    await async_wait_until(predicate, timeout=5)


def _kanban_app():
    app = FastAPI()
    app.include_router(sessions.list_router)
    return app


def _detail_app():
    app = FastAPI()
    app.include_router(sessions.manage_router)
    return app


@pytest.mark.asyncio
async def test_identical_kanban_task_ids_share_one_lane_read(monkeypatch):
    release = threading.Event()
    calls = []

    def slow_tasks(wanted, stale_ok=False):
        calls.append((tuple(wanted), stale_ok))
        assert release.wait(5)
        return [{"id": "t1", "status": "running"}]

    monkeypatch.setattr(sessions, "session_kanban_tasks", slow_tasks)
    app = _kanban_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        tasks = [asyncio.create_task(client.get("/api/session-kanban-tasks", params={"ids": "s1,s2"}))
                 for _ in range(8)]
        try:
            await _until(lambda: calls)
            await asyncio.sleep(0)  # let every request enter the in-flight wrapper
            assert calls == [(("s1", "s2"), True)]
        finally:
            release.set()
            responses = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in responses)
    bodies = [r.json() for r in responses]
    assert all(body == {"tasks": [{"id": "t1", "status": "running"}]} for body in bodies)


@pytest.mark.asyncio
async def test_distinct_kanban_task_ids_do_not_share_a_read(monkeypatch):
    gates = {key: threading.Event() for key in ("a", "b")}
    started = {key: threading.Event() for key in ("a", "b")}
    calls = []

    def gated_tasks(wanted, stale_ok=False):
        key = wanted[0]
        calls.append(key)
        started[key].set()
        assert gates[key].wait(5)
        return [{"id": key}]

    monkeypatch.setattr(sessions, "session_kanban_tasks", gated_tasks)
    app = _kanban_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.get("/api/session-kanban-tasks", params={"ids": "a"}))
        second = asyncio.create_task(client.get("/api/session-kanban-tasks", params={"ids": "b"}))
        try:
            await _until(lambda: started["a"].is_set() and started["b"].is_set())
            assert sorted(calls) == ["a", "b"]
        finally:
            gates["a"].set()
            gates["b"].set()
            responses = await asyncio.gather(first, second)
    assert {r.json()["tasks"][0]["id"] for r in responses} == {"a", "b"}


@pytest.mark.asyncio
async def test_kanban_tasks_missing_ids_is_422():
    app = _kanban_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/session-kanban-tasks")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_identical_detail_polls_share_one_display_annotation(monkeypatch):
    from hermes_cli.web_routers import cron

    release = threading.Event()
    started = threading.Event()
    calls = []

    class DB:
        def resolve_session_id(self, sid):
            return sid

        def get_session(self, sid):
            return {"id": sid}

        def get_compression_chain(self, sid):
            return [sid]

        def close(self):
            pass

    def annotate(rows, *, display=False):
        calls.append(display)
        started.set()
        assert release.wait(5)
        rows[0]["kanban_active"] = 3
        return rows

    monkeypatch.setattr(sessions, "_open_session_db_for_profile", lambda *a, **kw: DB())
    monkeypatch.setattr(sessions, "_serving_profile", lambda p: p or "default")
    monkeypatch.setattr(cron, "cron_run_scheduler_owned", lambda *a: None)
    monkeypatch.setattr(sessions, "annotate_kanban_activity", annotate)

    first = asyncio.create_task(sessions.get_session_detail("s1", "default"))
    await _until(started.is_set)
    second = asyncio.create_task(sessions.get_session_detail("s1", "default"))
    await _until(lambda: len(asyncio.all_tasks()) >= 2)
    await asyncio.sleep(0)  # let every request enter the in-flight wrapper
    assert calls == [True]
    release.set()
    rows = await asyncio.gather(first, second)
    assert all(row["id"] == "s1" and row["kanban_active"] == 3 and row["profile"] == "default" for row in rows)


@pytest.mark.asyncio
async def test_detail_deleted_session_is_still_404(monkeypatch):
    class DB:
        def resolve_session_id(self, sid):
            return sid

        def get_session(self, sid):
            return None

        def close(self):
            pass

    monkeypatch.setattr(sessions, "_open_session_db_for_profile", lambda *a, **kw: DB())
    with pytest.raises(HTTPException) as error:
        await sessions.get_session_detail("gone")
    assert error.value.status_code == 404
    assert error.value.detail == sessions._NOT_FOUND


@pytest.mark.asyncio
async def test_detail_http_shape_keeps_display_annotation(monkeypatch):
    from hermes_cli.web_routers import cron

    class DB:
        def resolve_session_id(self, sid):
            return sid

        def get_session(self, sid):
            return {"id": sid, "source": "cli"}

        def get_compression_chain(self, sid):
            return ["root", sid]

        def close(self):
            pass

    def annotate(rows, *, display=False):
        assert display is True
        rows[0]["kanban_blocked"] = 1
        return rows

    monkeypatch.setattr(sessions, "_open_session_db_for_profile", lambda *a, **kw: DB())
    monkeypatch.setattr(sessions, "_serving_profile", lambda p: "default")
    monkeypatch.setattr(cron, "cron_run_scheduler_owned", lambda *a: None)
    monkeypatch.setattr(sessions, "annotate_kanban_activity", annotate)
    app = _detail_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sessions/tip")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "tip"
    assert body["profile"] == "default"
    assert body["kanban_blocked"] == 1
    assert body["_lineage_ids"] == ["root", "tip"]
