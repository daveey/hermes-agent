"""Cold canonical enrichment must not gate the entire HTTP lifespan. [daveey]"""
import asyncio
from contextlib import asynccontextmanager
import threading

import httpx
import pytest

from hermes_cli.web_routers import profiles
from hermes_cli.sessions_native_startup import lifespan
from tests.hermes_cli import test_profiles_sidebar_scope as helpers
from tests.waiting import wait_until

profiles_on_disk = helpers.profiles_on_disk
_seed_session = helpers._seed_session
pytestmark = pytest.mark.platforms('linux')


@pytest.fixture(autouse=True)
def inprocess_annotation_faults(monkeypatch):
    # Events injected below exercise the original holder/drain boundary.
    # Real child custody/framing has separate process integration tests.
    from hermes_cli.sessions_native_startup import SessionsRuntime
    from hermes_cli.sessions_capture_worker import capture
    original = SessionsRuntime.__init__
    def initialize(self, profiles, targets, **kwargs):
        original(self, profiles, targets, capture_reader=capture, **kwargs)
    monkeypatch.setattr(SessionsRuntime, '__init__', initialize)


@pytest.mark.asyncio
async def test_real_lifespan_serves_while_canonical_annotation_is_cold(profiles_on_disk, monkeypatch):
    from hermes_cli import web_server, web_server_app

    entered, release = threading.Event(), threading.Event()
    original = profiles.annotate_kanban_activity

    def delayed(rows, *args, **kwargs):
        entered.set()
        assert release.wait(10), 'test must release canonical annotation'
        return original(rows, *args, **kwargs)

    @asynccontextmanager
    async def quiet_services(app):
        yield

    monkeypatch.setattr(profiles, 'annotate_kanban_activity', delayed)
    monkeypatch.setattr(web_server_app, 'standalone_lifespan', quiet_services)
    monkeypatch.setenv('HERMES_FACTS_LINKS', '0')
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '0')
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    _seed_session(profiles_on_disk['default'], 'cold-entry', source='cli')
    app = web_server.app
    context = app.router.lifespan_context(app)
    opening = asyncio.create_task(context.__aenter__())
    started = False
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        done, _ = await asyncio.wait({opening}, timeout=2)
        assert opening in done, 'cold annotation blocked the entire serve lifespan'
        await opening
        started = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}) as client:
            assert (await client.get('/api/health')).status_code == 200
            for path in ('/api/profiles/sessions', '/api/profiles/sessions/sidebar',
                         '/api/profiles/sessions/stamps?profile=default&ids=cold-entry'):
                pending = await client.get(path)
                assert pending.status_code == 503
                assert pending.headers['retry-after'] == '1'
                assert 'unavailable' in pending.text
            release.set()
            from tests.waiting import wait_until
            assert await asyncio.to_thread(wait_until,
                lambda: app.state.sessions_facts.binding is not None
                and app.state.sessions_facts.binding._mode == 'facts', timeout=8)
            response = await client.get('/api/profiles/sessions')
            assert response.status_code == 200
            assert any(row['id'] == 'cold-entry' for row in response.json()['sessions'])
            assert (await client.get('/api/profiles/sessions?profile=foreign')).status_code == 404
    finally:
        release.set()
        if not started:
            await opening
        await context.__aexit__(None, None, None)
    assert app.state.sessions_facts is None


@pytest.mark.asyncio
async def test_cold_shutdown_retains_real_capture_custody(profiles_on_disk, monkeypatch):
    from fastapi import FastAPI
    from tests.waiting import wait_until

    entered, release = threading.Event(), threading.Event()
    original = profiles.annotate_kanban_activity

    def delayed(rows, *args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(profiles, 'annotate_kanban_activity', delayed)
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    _seed_session(profiles_on_disk['default'], 'shutdown-entry', source='cli')
    app = FastAPI()
    context = lifespan(app)
    await context.__aenter__()
    runtime = app.state.sessions_facts
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(TimeoutError, match='source capture did not stop'):
            await context.__aexit__(None, None, None)
        assert app.state.sessions_facts is runtime
        assert not runtime.ready
        assert not runtime._bootstrap.is_alive()
        assert runtime.feed._thread.is_alive()
        assert runtime.binding is not None
        with pytest.raises(RuntimeError, match='custody retained'):
            async with lifespan(app):
                pytest.fail('retained native owner was replaced')
    finally:
        release.set()
        # A different app lease may keep the shared scheduler alive. Fence this
        # owner's real capture before retrying its retained shutdown instead.
        registrations = tuple(runtime.feed._registrations)
        assert registrations
        assert await asyncio.to_thread(wait_until,
            lambda: all(not bound.capturing for bound, _ in registrations), timeout=5)
        await asyncio.to_thread(runtime.close)
        assert runtime.feed._closed
        assert not runtime.feed._registrations
    assert runtime.binding is None
    assert not runtime.ready  # late canonical completion cannot activate after stop


@pytest.mark.asyncio
async def test_unavailable_capture_retries_without_replacing_owner(profiles_on_disk, monkeypatch):
    from fastapi import FastAPI
    from hermes_cli.sessions_native_startup import SessionsMiddleware
    from hermes_cli.fact_contract import Unavailable
    from tests.hermes_cli.test_sessions_native_startup import selected

    failed = threading.Event()
    original = profiles.annotate_kanban_activity

    def first_unavailable(rows, *args, **kwargs):
        if not failed.is_set():
            failed.set()
            raise OSError('cold optional enrichment unavailable')
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(profiles, 'annotate_kanban_activity', first_unavailable)
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    _seed_session(profiles_on_disk['default'], 'retry-entry', source='cli')
    app = FastAPI()
    app.add_middleware(SessionsMiddleware)
    app.include_router(profiles.sessions_router)
    async with lifespan(app):
        runtime = app.state.sessions_facts
        assert await asyncio.to_thread(wait_until, lambda: isinstance(
            runtime.store.snapshot(runtime.scope).view('sessions', selected()), Unavailable), timeout=5)
        assert runtime.feed.budget.max_seconds == 5
        assert not runtime.ready
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/profiles/sessions')).status_code == 503
            assert await asyncio.to_thread(wait_until, lambda: runtime.ready, timeout=10)
            response = await client.get('/api/profiles/sessions')
            assert response.status_code == 200
            assert any(row['id'] == 'retry-entry' for row in response.json()['sessions'])
        assert app.state.sessions_facts is runtime
    assert not runtime._bootstrap.is_alive()
    assert runtime.feed._closed
    assert not runtime.feed._registrations


@pytest.mark.asyncio
async def test_failed_bootstrap_before_binding_never_admits_legacy(profiles_on_disk, monkeypatch):
    from fastapi import FastAPI
    from hermes_cli.sessions_native_startup import SessionsMiddleware, SessionsRuntime

    def fail(self):
        raise OSError('native initialization failed before binding')

    monkeypatch.setattr(SessionsRuntime, 'start', fail)
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    app = FastAPI()
    app.add_middleware(SessionsMiddleware)
    app.include_router(profiles.sessions_router)
    async with lifespan(app):
        runtime = app.state.sessions_facts
        assert await asyncio.to_thread(wait_until, lambda: not runtime._bootstrap.is_alive(), timeout=5)
        assert runtime.binding is None
        monkeypatch.setattr(profiles, '_profile_targets', lambda *a: pytest.fail('legacy request I/O'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for path in ('sessions', 'sessions/sidebar', 'sessions/stamps?profile=default&ids=missing'):
                response = await client.get('/api/profiles/' + path)
                assert response.status_code == 503
                assert 'unavailable' in response.text
    assert app.state.sessions_facts is None


@pytest.mark.asyncio
async def test_cold_capture_beyond_old_bootstrap_deadline_eventually_ready(profiles_on_disk, monkeypatch):
    from fastapi import FastAPI

    entered, release = threading.Event(), threading.Event()
    original = profiles.annotate_kanban_activity

    def delayed(rows, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(25)
        return original(rows, *args, **kwargs)

    monkeypatch.setattr(profiles, 'annotate_kanban_activity', delayed)
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    _seed_session(profiles_on_disk['default'], 'slow-entry', source='cli')
    app = FastAPI()
    async with lifespan(app):
        runtime = app.state.sessions_facts
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            # Real elapsed time crosses capture5s and old bootstrap15s without
            # replacing clocks or relaxing the source's capture budget.
            await asyncio.to_thread(threading.Event().wait, 16)
            assert runtime._bootstrap.is_alive()
            assert not runtime.ready
            release.set()
            assert await asyncio.to_thread(wait_until, lambda: runtime.ready, timeout=40)
            assert runtime.feed.budget.max_seconds == 5
        finally:
            release.set()
    assert app.state.sessions_facts is None


@pytest.mark.asyncio
async def test_bootstrap_initialization_join_failure_retains_custody(profiles_on_disk, monkeypatch):
    from fastapi import FastAPI
    from hermes_cli.sessions_native_startup import SessionsRuntime

    entered, release = threading.Event(), threading.Event()
    original = SessionsRuntime.start

    def delayed(self):
        entered.set()
        assert release.wait(15)
        return original(self)

    monkeypatch.setattr(SessionsRuntime, 'start', delayed)
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    _seed_session(profiles_on_disk['default'], 'initializing-entry', source='cli')
    app = FastAPI()
    context = lifespan(app)
    await context.__aenter__()
    runtime = app.state.sessions_facts
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(TimeoutError, match='sessions bootstrap did not stop'):
            await context.__aexit__(None, None, None)
        assert app.state.sessions_facts is runtime
        assert runtime._bootstrap.is_alive()
        assert runtime.feed is None
    finally:
        release.set()
        assert await asyncio.to_thread(wait_until, lambda: not runtime._bootstrap.is_alive(), timeout=5)
        await asyncio.to_thread(runtime.close)
    assert not runtime.ready
    assert runtime.binding is None
    assert runtime.feed is not None
    assert runtime.feed._closed
    assert not runtime.feed._registrations
