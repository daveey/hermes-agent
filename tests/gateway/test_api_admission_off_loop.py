"""API admission never blocks the shared owner loop on a held SQLite writer (every HTTP door)."""
import asyncio
import sqlite3
import threading

import pytest
pytest.importorskip("aiohttp.test_utils", reason="aiohttp not installed (optional 'messaging' extra)")
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

KEY = 'off-loop-admission-key-long-enough'
WAIT_S = 30
AUTH = {'Authorization': 'Bearer ' + KEY}

DOORS = [
    ('chat', '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'hi'}]}, {}, 200),
    ('responses', '/v1/responses', {'input': 'hi'}, {'Idempotency-Key': 'resp-1'}, 200),
    ('runs', '/v1/runs', {'input': 'hi'}, {}, 202),
]


async def set_writer_trace(db, callback):
    """Swap the writer connection's trace callback only while no thread is inside a statement on it.

    ``set_trace_callback`` takes SQLite's connection mutex while holding the GIL, and a statement
    running in a worker thread holds that mutex while it calls the Python trace (which needs the
    GIL): swapping it mid-statement deadlocks the whole process. Writes still run after the HTTP
    response (the chat door's post-turn transaction), so take ``db._lock`` (every writer-connection
    user holds it) in a worker thread, never the loop, bounded by ``WAIT_S``.
    """
    def swap():
        with db._lock:
            db._conn.set_trace_callback(callback)
    await asyncio.wait_for(asyncio.to_thread(swap), WAIT_S)


@pytest.mark.asyncio
@pytest.mark.parametrize('name,path,body,headers,expected', DOORS, ids=[d[0] for d in DOORS])
async def test_admission_waits_for_the_writer_off_the_owner_loop(api, owner, monkeypatch, name, path, body,
                                                                headers, expected):
    from gateway.platforms import api_server_runs

    async def handle(event):
        from gateway.session_results import execution_result
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    async def no_execution(*args, **kwargs):
        pass
    owner.runner._handle_message = handle
    monkeypatch.setattr(api_server_runs, '_execute_run', no_execution)
    api._api_key = KEY
    app = web.Application()
    app.router.add_post(path, getattr(api, {'chat': '_handle_chat_completions', 'responses': '_handle_responses',
                                            'runs': '_handle_runs'}[name]))
    async with TestClient(TestServer(app)) as client:
        # Keep a real competing writer until the owner loop observes admission's
        # BEGIN. Wall-clock heartbeat gaps also measure runner descheduling, not
        # just loop blocking, so use ordering and thread identity instead.
        holder = sqlite3.connect(owner.db.db_path, timeout=0, isolation_level=None)
        holder.execute('BEGIN IMMEDIATE')
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        writer_started = asyncio.Event()
        writer_threads = []

        def trace(statement):
            if statement.upper().startswith('BEGIN') and not writer_threads:
                writer_threads.append(threading.get_ident())
                # Fail an inline-write regression without deadlocking the loop.
                # SQLite swallows trace callback exceptions; assert below instead.
                if writer_threads[0] == loop_thread:
                    holder.rollback()
                loop.call_soon_threadsafe(writer_started.set)

        await set_writer_trace(owner.db, trace)
        request = asyncio.create_task(client.post(path, json=body, headers={**AUTH, **headers}))
        try:
            await asyncio.wait_for(writer_started.wait(), WAIT_S)
            assert writer_threads[0] != loop_thread, 'admission write ran on the owner loop'
            assert holder.in_transaction
            assert not request.done(), 'admission finished before the competing writer released'
        finally:
            holder.rollback()
            holder.close()
            # Settle the HTTP task before restoring the trace or fixture state,
            # including assertion failures; no unjoined timer touches a closed DB.
            try:
                response = await asyncio.wait_for(request, WAIT_S)
            finally:
                await set_writer_trace(owner.db, None)
        assert response.status == expected, await response.text()
