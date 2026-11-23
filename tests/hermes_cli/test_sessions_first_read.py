"""Bounded native first demand returns real rows, never a fabricated empty. [daveey]"""
import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI

from hermes_cli.sessions_native_startup import SessionsMiddleware, lifespan
from hermes_cli.web_routers import profiles
from tests.hermes_cli import test_profiles_sidebar_scope as helpers
from tests.waiting import wait_until

profiles_on_disk = helpers.profiles_on_disk
pytestmark = pytest.mark.platforms('linux')


@pytest.mark.asyncio
async def test_cold_selector_waits_for_owner_then_warm_read_has_no_capture(profiles_on_disk, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = profiles.annotate_kanban_activity
    captures = []

    def gated(rows, *args, **kwargs):
        captures.append(1)
        if any(row['id'] == 'first-read-row' for row in rows) and armed.is_set():
            entered.set()
            assert release.wait(5)
        return original(rows, *args, **kwargs)

    armed = threading.Event()
    monkeypatch.setattr(profiles, 'annotate_kanban_activity', gated)
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    helpers._seed_session(profiles_on_disk['default'], 'first-read-row', source='cli')
    app = FastAPI()
    app.add_middleware(SessionsMiddleware)
    app.include_router(profiles.sessions_router)
    async with lifespan(app):
        runtime = app.state.sessions_facts
        assert await asyncio.to_thread(wait_until, lambda: runtime.ready, timeout=8)
        armed.set()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            request = asyncio.create_task(client.get('/api/profiles/sessions?profile=default&limit=7'))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                assert not request.done(), 'first demand returned before its real projection was ready'
                release.set()
                cold = await asyncio.wait_for(request, 4)
                assert cold.status_code == 200
                assert any(row['id'] == 'first-read-row' for row in cold.json()['sessions'])
                before = len(captures)
                warm = await client.get('/api/profiles/sessions?profile=default&limit=7')
                assert warm.status_code == 200
                assert warm.content == cold.content
                assert len(captures) == before
                assert (await client.get('/api/profiles/sessions?profile=foreign')).status_code == 404
            finally:
                release.set()
                await request


def _bound_reader(wait):
    from types import SimpleNamespace
    from hermes_cli.fact_contract import Scope
    from hermes_cli.fact_fake import FakeStore
    from hermes_cli.sessions_fact_routes import SessionsFactRoutes

    store, scope = FakeStore(), Scope('install', 'profile')
    bridge = SessionsFactRoutes().install(SimpleNamespace(
        v1=store, demand=lambda *args: None, wait_projection=wait), scope)
    bridge.on_mode('facts', 1)
    return bridge, store, scope


@pytest.mark.asyncio
async def test_exhausted_first_read_is_honest_503_and_wait_is_off_loop():
    from fastapi import HTTPException

    calls = []
    caller = threading.get_ident()

    def wait(name, selector, timeout):
        calls.append((name, selector, timeout, threading.get_ident()))

    bridge, _, _ = _bound_reader(wait)
    try:
        with pytest.raises(HTTPException) as error:
            await bridge.first_response('sessions', {'profile': 'default'})
        assert error.value.status_code == 503
        assert error.value.headers['Retry-After'] == '1'
        assert calls[0][:3] == ('sessions', {'profile': 'default'}, 4.0)
        assert calls[0][3] != caller
        assert len(calls) == 1
    finally:
        bridge.on_mode('legacy', 2)
        bridge.close()


@pytest.mark.asyncio
async def test_unavailable_never_waits_and_publication_race_returns_ready():
    from fastapi import HTTPException
    from hermes_cli.fact_contract import Fact

    bridge, store, scope = _bound_reader(lambda *args: pytest.fail('not Pending: no wait'))
    selector = {'profile': 'default'}
    key = store.view_key(scope, 'sessions', selector)
    try:
        store.publish([Fact(key, {}, error_code='capture_failed')])
        with pytest.raises(HTTPException) as error:
            await bridge.first_response('sessions', selector)
        assert error.value.status_code == 503
        store.publish([], deleted_keys=[key])
        body = b'{"sessions":[{"id":"actual-row"}]}'

        def publish(*args):
            store.publish([Fact(key, {'encoded': body}, health='fresh')])

        bridge._handle.demand = publish
        assert (await bridge.first_response('sessions', selector)).body == body
    finally:
        bridge.on_mode('legacy', 2)
        bridge.close()

