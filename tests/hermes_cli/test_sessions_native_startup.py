"""Real native source -> scoped store -> mounted HTTP, without request capture."""
import asyncio
import builtins
import inspect
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading
import time

import httpx
import pytest
from fastapi import FastAPI

from hermes_cli.sessions_fact_routes import ScopedSessionsRoutes, selector_for
from hermes_cli.sessions_native_startup import SessionsMiddleware, SessionsRuntime, lifespan
from hermes_cli.web_routers import profiles
from tests.hermes_cli import test_profiles_sidebar_scope as helpers
from tests.waiting import wait_until

profiles_on_disk = helpers.profiles_on_disk


@pytest.fixture
def native(profiles_on_disk, monkeypatch):
    monkeypatch.setattr(profiles, '_SIDEBAR_CACHE_TTL_SECONDS', 0)
    for name, home in profiles_on_disk.items():
        for i in range(4):
            helpers._seed_session(home, f'{name}-{i}', source=('cli', 'cron', 'telegram', 'cli')[i], pinned=i == 0)
    observed = time.time()
    monkeypatch.setattr(time, 'time', lambda: observed)
    runtime = SessionsRuntime(profiles, list(profiles_on_disk.items())).start()
    try:
        yield runtime
    finally:
        runtime.close()


def app_for(runtime):
    app = FastAPI()
    app.state.sessions_facts = runtime
    app.add_middleware(SessionsMiddleware)
    app.include_router(profiles.sessions_router)
    return app


def selected(**kwargs):
    return selector_for(inspect.signature(profiles.get_profiles_sessions), (), kwargs)


def wait_ready(runtime, selector):
    from hermes_cli.fact_contract import Ready
    assert wait_until(lambda: isinstance(runtime.store.snapshot(runtime.scope).view('sessions', selector), Ready), timeout=10)


@pytest.mark.asyncio
async def test_native_capture_mutation_and_warmed_http_no_io(native, monkeypatch):
    runtime = native
    selector = selected(limit=2, profile='worker')
    runtime.demand(runtime.scope, 'sessions', selector)
    await asyncio.to_thread(wait_ready, runtime, selector)
    expected = profiles.get_profiles_sessions(**selector)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(runtime)), base_url='http://test')
    async with client:
        first = await client.get('/api/profiles/sessions', params={'limit': 2, 'profile': 'worker'})
        assert first.status_code == 200
        assert first.json() == json.loads(json.dumps(expected)), [(k, row.get(k), expected['sessions'][i].get(k)) for i, row in enumerate(first.json()['sessions']) for k in row if row.get(k) != expected['sessions'][i].get(k)]
        request_thread = threading.get_ident()
        def guard(original):
            def checked(*args, **kwargs):
                assert threading.get_ident() != request_thread, 'request performed I/O or legacy capture'
                return original(*args, **kwargs)
            return checked
        with monkeypatch.context() as patch:
            for owner, names in ((builtins, ('open',)), (io, ('open',)),
                                 (Path, ('stat', 'read_text', 'read_bytes', 'open', 'iterdir')),
                                 (sqlite3, ('connect',)), (subprocess, ('Popen', 'run')),
                                 (socket.socket, ('connect',)),
                                 (profiles, ('_profile_targets', '_read_profile_db', '_read_serial_profile_db',
                                             'annotate_kanban_activity', 'annotate_goal_activity', 'annotate_cron_activity',
                                             'annotate_background_activity', 'annotate_turn_activity',
                                             'annotate_subagent_activity', 'annotate_review_wait'))):
                for name in names:
                    patch.setattr(owner, name, guard(getattr(owner, name)))
            for _ in range(20):
                response = await client.get('/api/profiles/sessions', params={'limit': 2, 'profile': 'worker'})
                assert response.status_code == 200
                assert response.content == first.content
        # Actual state.db write, no explicit source invalidation/demand afterward.
        from hermes_state import SessionDB
        home = dict(runtime.targets)['worker']
        db = SessionDB(db_path=home / 'state.db')
        try:
            db.set_session_title('worker-0', 'native changed')
            def changed():
                answer = runtime.store.snapshot(runtime.scope).view('sessions', selector)
                return hasattr(answer, 'encoded') and b'native changed' in answer.encoded
            assert await asyncio.to_thread(wait_until, changed, timeout=10)
        finally:
            db.close()


@pytest.mark.asyncio
async def test_cold_demand_foreign_scope_and_rollback(native):
    runtime = native
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(runtime)), base_url='http://test') as client:
        response = await client.get('/api/profiles/sessions', params={'profile': 'worker', 'limit': 1})
        assert response.status_code == 503
        await asyncio.to_thread(wait_ready, runtime, selected(profile='worker', limit=1))
        assert (await client.get('/api/profiles/sessions', params={'profile': 'worker', 'limit': 1})).status_code == 200
        assert (await client.get('/api/profiles/sessions', params={'profile': 'foreign'})).status_code == 404
        runtime.lifecycle.transition('sessions', runtime.scope, 'legacy', 10)
        runtime.generation = 10
        runtime.binding.on_mode('legacy', 10)
        assert (await client.get('/api/profiles/sessions', params={'profile': 'worker', 'limit': 1})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('name,path,params,reader', [
    ('sessions', '/api/profiles/sessions', {'offset': 3, 'limit': 1, 'min_messages': 1}, 'get_profiles_sessions'),
    ('sessions', '/api/profiles/sessions', {'full': True, 'source': 'cli', 'order': 'created'}, 'get_profiles_sessions'),
    ('sessions', '/api/profiles/sessions', {'archived': 'only', 'automated': 'exclude'}, 'get_profiles_sessions'),
    ('sessions_sidebar', '/api/profiles/sessions/sidebar', {'recents_profile': 'worker'}, 'get_profiles_sessions_sidebar'),
    ('sessions_sidebar', '/api/profiles/sessions/sidebar', {'recents_profile': 'all', 'cron_limit': 1}, 'get_profiles_sessions_sidebar'),
    ('sessions_stamps', '/api/profiles/sessions/stamps', {'profile': 'worker', 'ids': 'worker-0,missing,worker-2'}, 'get_profiles_session_stamps'),
])
async def test_all_mounted_views_native_parity(native, name, path, params, reader):
    from hermes_cli.fact_contract import Ready
    fn = getattr(profiles, reader)
    selector = selector_for(inspect.signature(fn), (), params)
    expected = inspect.unwrap(fn)(**selector)
    native.demand(native.scope, name, selector)
    def ready():
        return isinstance(native.store.snapshot(native.scope).view(name, selector), Ready)
    assert await asyncio.to_thread(wait_until, ready, timeout=10)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(native)), base_url='http://test') as client:
        response = await client.get(path, params=params)
        assert response.status_code == 200
        assert response.json() == json.loads(json.dumps(expected))


@pytest.mark.asyncio
async def test_lifespan_off_and_reentry(profiles_on_disk, monkeypatch):
    monkeypatch.setattr(profiles, '_SESSION_FACT_ROUTES', ScopedSessionsRoutes())
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '0')
    app = FastAPI()
    async with lifespan(app):
        assert getattr(app.state, 'sessions_facts', None) is None
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    for _ in range(2):
        async with lifespan(app):
            assert app.state.sessions_facts.binding._mode == 'facts'
        assert app.state.sessions_facts is None


def test_real_app_lifespan_and_middleware(profiles_on_disk, monkeypatch):
    from contextlib import asynccontextmanager
    from fastapi.testclient import TestClient
    from hermes_cli import web_server, web_server_app
    @asynccontextmanager
    async def quiet_services(app):
        yield
    # Only unrelated background services are suppressed; actual sessions lifespan
    # and registered HTTP middleware must compose through the real app entry.
    monkeypatch.setattr(web_server_app, 'standalone_lifespan', quiet_services)
    monkeypatch.setenv('HERMES_FACTS_LINKS', '0')
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '0')  # annotations have separate real composition coverage
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    helpers._seed_session(profiles_on_disk['default'], 'app-entry', source='cli')
    with TestClient(web_server.app) as client:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        assert web_server.app.state.sessions_facts.binding._mode == 'facts'
        response = client.get('/api/profiles/sessions')
        assert response.status_code == 200
        assert any(row['id'] == 'app-entry' for row in response.json()['sessions'])
    assert web_server.app.state.sessions_facts is None
