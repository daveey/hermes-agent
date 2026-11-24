"""Committed paint and red-edge reports cannot queue behind unrelated asyncio work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import socket
import threading

from fastapi import FastAPI
import httpx
import pytest
import uvicorn

from hermes_cli import desktop_stripe_report as reports
from hermes_cli.web_routers import sessions
from hermes_state import SessionDB
from tests.waiting import async_wait_until


@asynccontextmanager
async def http_peer():
    app = FastAPI()
    app.include_router(sessions.manage_router)

    @app.get('/control')
    async def control():
        return {'ok': True}

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error'))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await async_wait_until(lambda: server.started, timeout=5, what='HTTP listener')
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{listener.getsockname()[1]}',
                                     trust_env=False, timeout=3) as client:
            yield client
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()


@pytest.fixture
def stored_status(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'state.db')
    canonical = {'status_version': 1, 'identity': {'gateway': 'g', 'profile': 'default',
                 'session_id': 'chat', 'owner_epoch': 'e'}, 'revision': 4, 'generation': 2,
                 'coverage': {'state': 'complete', 'reasons': []},
                 'presentation': {'stripe': 'done', 'reason_codes': []}}
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('chat', source='test')
        db._execute_write(lambda c: c.execute('UPDATE sessions SET outcome_json=? WHERE id=?',
                                             (json.dumps({'kind': 'done', 'status': canonical}), 'chat')))
    return canonical


def paint(canonical, order):
    return {'status_version': 1, 'status': 'canonical', 'identity': canonical['identity'],
            'revision': 4, 'generation': 2, 'coverage': canonical['coverage'], 'stripe': 'done',
            'surface': 'row', 'mounted': True, 'observation_client': 'client',
            'observation_lifetime': 1, 'observation_order': order}


@pytest.mark.asyncio
@pytest.mark.parametrize('qualified', [False, True], ids=['reasons', 'committed-paint'])
async def test_reports_finish_while_default_executor_is_occupied(stored_status, tmp_path, qualified):
    """Real TCP, real DB/probes, real log append/comparison; only unrelated work is gated.

    Read the successful responses and disk evidence BEFORE releasing the default executor.
    The independent async control distinguishes executor queueing from an unresponsive server.
    """
    loop = asyncio.get_running_loop()
    prior = loop._default_executor
    pool = ThreadPoolExecutor(max_workers=2)
    release = threading.Event()
    started = [threading.Event(), threading.Event()]

    def occupy(event):
        event.set()
        assert release.wait(10), 'fixture executor was not released'

    async with http_peer() as client:
        loop.set_default_executor(pool)
        blockers = [pool.submit(occupy, event) for event in started]
        calls = []
        try:
            # Neither this readiness check nor the control uses the occupied executor.
            await async_wait_until(lambda: all(event.is_set() for event in started),
                                   timeout=3, what='occupied default executor')
            calls = [asyncio.create_task(client.post('/api/sessions/chat/stripe-report', json=(
                paint(stored_status, i + 1) if qualified else
                {'stripe': 'stuck', 'reasons': [f'edge:{i}']}))) for i in range(8)]
            control = await client.get('/control')
            assert control.json() == {'ok': True}
            responses = await asyncio.wait_for(asyncio.gather(*calls), 2)
            assert not release.is_set()
            assert all(response.status_code == 200 for response in responses)
            lines = [json.loads(line) for line in reports.report_path(tmp_path).read_text().splitlines()]
            assert len(lines) == 8
            if qualified:
                assert all(response.json()['comparison']['state'] == 'agreement' for response in responses)
                assert {line['observation_order'] for line in lines} == set(range(1, 9))
                assert reports.surface_observations(tmp_path, ['chat'], stored_status)['row']['state'] == 'agreement'
            else:
                assert all('comparison' not in response.json() for response in responses)
                assert {line['reasons'][0] for line in lines} == {f'edge:{i}' for i in range(8)}
        finally:
            release.set()
            for call in calls:
                if not call.done():
                    call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            for blocker in blockers:
                blocker.result(timeout=3)
            loop._default_executor = prior
            pool.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('qualified', [False, True], ids=['reasons', 'committed-paint'])
async def test_full_stripe_lane_refuses_explicitly_and_waits_for_evidence(
        stored_status, tmp_path, monkeypatch, qualified):
    from hermes_cli import web_thread_lanes as lanes

    # Fixture-local short queue deadline, not a production timeout change.
    monkeypatch.setattr(lanes, '_lanes', lanes._lanes)
    monkeypatch.setattr(lanes, '_deadline_s', lanes._deadline_s)
    lanes.configure(lanes.DEFAULT_SIZES, queue_deadline_s=.05)
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]
    lock = threading.Lock()
    count = 0
    name = 'surface_observations' if qualified else 'record'
    original = getattr(reports, name)

    def blocked(*args, **kwargs):
        nonlocal count
        with lock:
            index = count
            count += 1
        entered[index].set()
        assert release.wait(10), 'fixture evidence work was not released'
        return original(*args, **kwargs)

    monkeypatch.setattr(reports, name, blocked)
    body = paint(stored_status, 1) if qualified else {'stripe': 'stuck', 'reasons': ['edge']}
    calls = []
    async with http_peer() as client:
        try:
            calls = [asyncio.create_task(client.post('/api/sessions/chat/stripe-report', json=body))
                     for _ in range(2)]
            await async_wait_until(lambda: all(event.is_set() for event in entered),
                                   timeout=3, what='occupied stripe lane')
            assert not any(call.done() for call in calls)  # no success before comparison/append
            rejected = await client.post('/api/sessions/chat/stripe-report', json=body)
            assert rejected.status_code == 503
            assert rejected.headers['retry-after'] == '2'
            assert rejected.json()['detail']['lane'] == 'stripe'
            assert rejected.json()['detail']['reason'] == 'deadline'
            assert count == 2  # the refused report never ran, and was not silently acknowledged
            assert (await client.get('/control')).json() == {'ok': True}
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*calls), 3)
            assert all(response.status_code == 200 for response in responses)
            assert len(reports.report_path(tmp_path).read_text().splitlines()) == 2
            if qualified:
                assert all(response.json()['comparison']['state'] == 'agreement' for response in responses)
        finally:
            release.set()
            await asyncio.gather(*calls, return_exceptions=True)
