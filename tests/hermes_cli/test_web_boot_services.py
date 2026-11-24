"""Native sessions can answer while unrelated standalone services warm."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest


@pytest.mark.asyncio
async def test_services_wait_for_listener_and_gate_unready_routes():
    from hermes_cli.web_boot_services import BootServicesMiddleware, services_lifespan
    app = FastAPI()
    app.state.http_listener_ready = asyncio.Event()
    app.state.sessions_facts = SimpleNamespace()
    app.add_middleware(BootServicesMiddleware)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def services(app):
        entered.set()
        await release.wait()
        try:
            yield
        finally:
            closed.set()

    @app.get('/api/health')
    @app.get('/api/profiles/sessions')
    @app.get('/api/profiles/sessions/sidebar')
    @app.get('/api/private')
    async def response():
        return {'sessions': [{'id': 'real-row'}]}

    async with services_lifespan(app, services):
        assert not entered.is_set()
        app.state.http_listener_ready.set()
        await asyncio.wait_for(entered.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            for path in ('/api/health', '/api/profiles/sessions', '/api/profiles/sessions/sidebar'):
                assert (await client.get(path)).status_code == 200
            pending = await client.get('/api/private')
            assert pending.status_code == 503
            assert pending.headers['retry-after'] == '1'
            assert (await client.post('/api/health')).status_code == 503
            assert (await client.get('/api/profiles/sessions/unsafe')).status_code == 503
            release.set()
            await asyncio.wait_for(app.state.boot_services_ready.wait(), 2)
            assert (await client.get('/api/private')).status_code == 200
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['legacy', 'embedded', 'no_listener'])
async def test_other_startup_modes_still_wait_for_services(mode):
    from hermes_cli.web_boot_services import services_lifespan
    app = FastAPI()
    app.state.http_listener_ready = asyncio.Event() if mode != 'no_listener' else None
    app.state.sessions_facts = None if mode == 'legacy' else SimpleNamespace()
    app.state.gateway_runner = object() if mode == 'embedded' else None
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def services(app):
        entered.set()
        await release.wait()
        yield

    context = services_lifespan(app, services)
    opening = asyncio.create_task(context.__aenter__())
    await asyncio.wait_for(entered.wait(), 2)
    assert not opening.done()
    release.set()
    await opening
    await context.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_shutdown_before_bind_does_not_start_services():
    from hermes_cli.web_boot_services import services_lifespan
    app = FastAPI()
    app.state.http_listener_ready = asyncio.Event()
    app.state.sessions_facts = SimpleNamespace()

    @asynccontextmanager
    async def services(app):
        pytest.fail('services ran before listener')
        yield

    async with services_lifespan(app, services):
        pass
    assert app.state.boot_services_ready is None


@pytest.mark.asyncio
async def test_real_native_lifespan_keeps_health_open_while_services_warm(monkeypatch):
    from hermes_cli import web_server, web_server_app
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    monkeypatch.setenv('HERMES_FACTS_LINKS', '0')
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '0')
    monkeypatch.setenv('HERMES_FACTS_BOARD', '0')
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def services(app):
        entered.set()
        await release.wait()
        yield

    monkeypatch.setattr(web_server_app, 'standalone_lifespan', services)
    app = web_server.app
    monkeypatch.setattr(app.state, 'http_listener_ready', asyncio.Event(), raising=False)
    async with app.router.lifespan_context(app):
        assert app.state.sessions_facts is not None
        assert not entered.is_set()
        app.state.http_listener_ready.set()
        await asyncio.wait_for(entered.wait(), 2)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test',
                    headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}) as client:
                assert (await client.get('/api/health')).status_code == 200
                pending = await client.get('/api/models')
                assert pending.status_code == 503
                assert 'warming' in pending.text
        finally:
            release.set()


@pytest.mark.asyncio
async def test_failed_warmup_remains_fail_closed(caplog):
    from hermes_cli.web_boot_services import BootServicesMiddleware, services_lifespan
    app = FastAPI()
    app.state.http_listener_ready = asyncio.Event()
    app.state.sessions_facts = SimpleNamespace()
    app.add_middleware(BootServicesMiddleware)
    failed = asyncio.Event()

    @asynccontextmanager
    async def services(app):
        failed.set()
        raise RuntimeError('warmup failed')
        yield

    with pytest.raises(RuntimeError, match='warmup failed'):
        async with services_lifespan(app, services):
            app.state.http_listener_ready.set()
            await failed.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                assert (await client.get('/api/models')).status_code == 503
            assert not app.state.boot_services_ready.is_set()
    await asyncio.sleep(0)  # deliver the completed task's logging callback
    assert 'routes remain unavailable' in caplog.text


@pytest.mark.asyncio
async def test_ws_refused_during_warmup():
    from hermes_cli.web_boot_services import BootServicesMiddleware
    ready = asyncio.Event()
    app = SimpleNamespace(state=SimpleNamespace(boot_services_ready=ready))
    sent = []
    async def forbidden(*args):
        pytest.fail('uninitialized websocket handler called')
    async def send(message):
        sent.append(message)
    async def receive():
        return {'type': 'websocket.connect'}
    await BootServicesMiddleware(forbidden)({'type': 'websocket', 'app': app}, receive, send)
    assert sent == [{'type': 'websocket.accept'}, {'type': 'websocket.close', 'code': 1013}]


@pytest.mark.asyncio
async def test_cancelled_half_start_shutdown_keeps_custody():
    from hermes_cli.web_boot_services import services_lifespan
    app = FastAPI()
    app.state.http_listener_ready = asyncio.Event()
    app.state.sessions_facts = SimpleNamespace()
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def services(app):
        entered.set()
        await release.wait()
        try:
            yield
        finally:
            closed.set()

    context = services_lifespan(app, services)
    await context.__aenter__()
    app.state.http_listener_ready.set()
    await entered.wait()
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    # One event-loop turn ensures shutdown is shield-waiting on the half-start.
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    assert app.state.boot_services_ready is not None
    assert not closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert closed.is_set()
    assert app.state.boot_services_ready is None
