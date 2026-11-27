"""Headless plugin startup belongs to the post-listener service owner."""
import sys
import threading

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
import httpx
import pytest

from hermes_cli import web_boot_services as boot


@pytest.fixture
def mount_env(monkeypatch, tmp_path):
    from hermes_cli import web_server as server, web_server_dashboard as dashboard
    from hermes_cli import plugin_isolation, plugins_cmd
    from hermes_cli.fact_plugin_mount import lifecycles
    app = FastAPI()
    plugins, modules = [], {}
    monkeypatch.setattr(server, 'app', app)
    monkeypatch.setattr(server, '_get_dashboard_plugins', lambda: plugins)
    monkeypatch.setattr(plugins_cmd, '_get_enabled_set', lambda: set(modules))
    monkeypatch.setattr(plugins_cmd, '_get_disabled_set', lambda: set())
    monkeypatch.setattr(plugin_isolation, 'import_user_code', lambda path, name: modules[path.parent.name])
    monkeypatch.setattr(dashboard, '_plugin_route_secret_scope', lambda: None)
    lifecycles(app)

    def add(name, router=None, start=None):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'api.py').touch()
        router = router or APIRouter()
        router.add_api_route('/ping', lambda: {'ok': True})
        module = SimpleNamespace(router=router)
        if start is not None:
            module.start_legacy = start
        modules[name] = module
        plugins.append(dict(name=name, source='bundled', _dir=str(folder), _api_file='api.py'))
        return folder / 'api.py'

    return app, dashboard, add


@pytest.mark.parametrize('deferred', [True, False])
def test_import_sweep_preserves_fallback_position(mount_env, monkeypatch, deferred):
    app, dashboard, add = mount_env
    add('probe')
    app.add_api_route('/auth', lambda: {'shell': True})
    del app.state.fact_plugin_lifecycles
    monkeypatch.setattr(boot, '_defer_plugin_api_sweep', deferred, raising=False)
    dashboard._mount_plugin_api_routes()
    app.add_api_route('/{full_path:path}', lambda: {'shell': True})
    paths = [r.path for r in app.routes][4:]
    expected = ['/auth'] + ([] if deferred else ['/api/plugins/probe/ping'])
    assert paths == expected + ['/api/plugins/{name}/{path:path}', '/{full_path:path}']
    assert getattr(app.state, 'plugin_api_sweep_owed', False) is deferred
    if deferred:
        assert app.state.fact_plugin_lifecycles is not None


@pytest.mark.parametrize('headless', [True, False])
def test_run_server_defers_only_headless(monkeypatch, headless):
    from hermes_cli.dashboard_server_start import run_server
    from hermes_cli.observability import shared_metrics_process
    calls = []
    monkeypatch.setattr(boot, 'defer_plugin_api_sweep', lambda: calls.append('defer'), raising=False)
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', SimpleNamespace(start_server=lambda **kw: calls.append('start')))
    monkeypatch.setattr(shared_metrics_process, 'begin_process', lambda *args: None)
    run_server(SimpleNamespace(host='localhost', port=1, no_open=True), headless=headless,
               owner=None, prepare_auth=lambda args: calls.append('auth'), bootstrap={}, mcp_after_bind=None)
    assert calls == (['defer'] if headless else []) + ['auth', 'start']


@pytest.mark.asyncio
@pytest.mark.parametrize('warm', [True, False])
async def test_sweep_lifespans_are_owned_and_published_after_services(mount_env, warm):
    app, dashboard, add = mount_env
    events = []
    owners = {}

    def record(name, phase):
        task = asyncio.current_task()
        if phase == 'enter':
            owners[name] = task
        else:
            assert task is owners[name]
        events.append((name, phase))

    async def startup():
        record('default', 'enter')

    async def shutdown():
        record('default', 'exit')

    @asynccontextmanager
    async def custom(app):
        record('custom', 'enter')
        yield
        record('custom', 'exit')

    add('default', APIRouter(on_startup=[startup], on_shutdown=[shutdown]),
        start=lambda: (events.append(('legacy', 'start')) or SimpleNamespace(close=lambda: None)))
    add('custom', APIRouter(lifespan=custom))
    dashboard._register_plugin_api_fallback(app)
    app.add_api_route('/{full_path:path}', lambda: {'shell': True})
    original = app.router.lifespan_context
    app.state.plugin_api_sweep_owed = True
    app.openapi_schema = {'stale': True}
    if warm:
        app.state.http_listener_ready = asyncio.Event()
        app.state.sessions_facts = object()
    app.add_middleware(boot.BootServicesMiddleware)

    @asynccontextmanager
    async def services(app):
        # The kanban legacy start launches board prewarm before hold_bind_for_prewarm joins it.
        assert events == [('legacy', 'start')]
        assert not dashboard._plugin_api_mounted(app, 'default')
        record('services', 'enter')
        yield
        record('services', 'exit')

    async with boot.services_lifespan(app, services):
        if warm:
            assert events == []
            app.state.http_listener_ready.set()
            await asyncio.wait_for(app.state.boot_services_ready.wait(), 3)
        assert events == [('legacy', 'start'), ('services', 'enter'), ('default', 'enter'), ('custom', 'enter')]
        assert owners['default'] is owners['custom'] is owners['services']
        assert app.router.lifespan_context is original
        assert app.state.plugin_api_sweep_owed is False
        assert app.openapi_schema is None
        paths = [r.path for r in app.routes]
        assert paths.index('/api/plugins/custom/ping') < paths.index('/api/plugins/{name}/{path:path}')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            assert (await client.get('/api/plugins/default/ping')).status_code == 200
    assert events[-3:] == [('custom', 'exit'), ('default', 'exit'), ('services', 'exit')]


@pytest.mark.asyncio
async def test_requests_gated_before_listener_and_during_scoped_sweep(mount_env, monkeypatch, caplog):
    from agent.secret_scope import current_secret_scope_home
    from hermes_constants import get_process_hermes_home
    from hermes_cli import plugins_cmd
    app, dashboard, add = mount_env
    add('probe')
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    reads = []

    def enabled():
        assert threading.get_ident() != main_thread
        assert current_secret_scope_home() == str(get_process_hermes_home())
        reads.append('enabled')
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return {'probe'}

    def disabled():
        assert current_secret_scope_home() == str(get_process_hermes_home())
        reads.append('disabled')
        return set()

    monkeypatch.setattr(plugins_cmd, '_get_enabled_set', enabled)
    monkeypatch.setattr(plugins_cmd, '_get_disabled_set', disabled)
    app.add_api_route('/api/profiles/sessions', lambda: {'sessions': []})
    dashboard._register_plugin_api_fallback(app)
    app.state.plugin_api_sweep_owed = True
    app.state.http_listener_ready = asyncio.Event()
    app.state.sessions_facts = object()
    app.add_middleware(boot.BootServicesMiddleware)

    @asynccontextmanager
    async def services(app):
        yield

    with caplog.at_level('INFO', logger=boot.__name__):
        async with boot.services_lifespan(app, services):
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                    for during in (False, True):
                        if during:
                            app.state.http_listener_ready.set()
                            await asyncio.wait_for(entered.wait(), 3)
                        response = await client.get('/api/plugins/probe/ping')
                        assert response.status_code == 503
                        assert response.headers['retry-after'] == '1'
                        assert (await client.get('/api/profiles/sessions')).status_code == 200
                        assert not dashboard._plugin_api_mounted(app, 'probe')
                    release.set()
                    await asyncio.wait_for(app.state.boot_services_ready.wait(), 3)
                    assert (await client.get('/api/plugins/probe/ping')).status_code == 200
            finally:
                release.set()
    assert reads == ['enabled', 'disabled']
    lines = [r.message for r in caplog.records if 'Deferred plugin API sweep:' in r.message]
    assert len(lines) == 1
    assert 'mounted=1 wall=' in lines[0] and 'thread_cpu=' in lines[0]


@pytest.mark.asyncio
async def test_failed_router_lifespan_is_stopped_without_publication(mount_env, caplog):
    app, dashboard, add = mount_env
    stopped = []

    @asynccontextmanager
    async def broken(app):
        assert not dashboard._plugin_api_mounted(app, 'broken')
        raise RuntimeError('broken enter')
        yield

    path = add('broken', APIRouter(lifespan=broken),
               start=lambda: SimpleNamespace(close=lambda: stopped.append('broken')))
    add('good')
    dashboard._register_plugin_api_fallback(app)
    app.state.plugin_api_sweep_owed = True
    app.state.http_listener_ready = asyncio.Event()
    app.state.sessions_facts = object()

    @asynccontextmanager
    async def services(app):
        yield

    async with boot.services_lifespan(app, services):
        app.state.http_listener_ready.set()
        await asyncio.wait_for(app.state.boot_services_ready.wait(), 3)
        assert stopped == ['broken']
        assert path.resolve() not in app.state.fact_plugin_lifecycles._handles
        assert not dashboard._plugin_api_mounted(app, 'broken')
        assert dashboard._plugin_api_mounted(app, 'good')
    assert 'Failed to enter plugin broken router lifespan' in caplog.text


def test_closed_registry_refuses_late_sweep(mount_env, caplog):
    app, dashboard, add = mount_env
    started = []
    add('late', start=lambda: started.append('leaked'))
    registry = app.state.fact_plugin_lifecycles
    registry.close()
    assert boot._sweep_plugin_apis(app) == []
    assert started == []
    assert registry._handles == {}
    assert not dashboard._plugin_api_mounted(app, 'late')
    assert 'custody retained or closed' in caplog.text


def test_defer_switch_refuses_an_already_imported_server(monkeypatch):
    monkeypatch.setattr(boot, '_defer_plugin_api_sweep', False, raising=False)
    monkeypatch.delitem(sys.modules, 'hermes_cli.web_server', raising=False)
    assert boot.defer_plugin_api_sweep() is True
    assert boot._defer_plugin_api_sweep is True
    monkeypatch.setattr(boot, '_defer_plugin_api_sweep', False)
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', object())
    assert boot.defer_plugin_api_sweep() is False
    assert boot._defer_plugin_api_sweep is False
