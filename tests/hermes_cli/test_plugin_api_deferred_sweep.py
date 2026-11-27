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
    monkeypatch.setattr(server, '_get_dashboard_plugins', lambda **kwargs: plugins)  # the hot mount passes force_rescan
    monkeypatch.setattr(plugins_cmd, '_get_enabled_set', lambda: set(modules))
    monkeypatch.setattr(plugins_cmd, '_get_disabled_set', lambda: set())
    monkeypatch.setattr(plugin_isolation, 'import_user_code', lambda path, name: modules[path.parent.name])
    monkeypatch.setattr(dashboard, '_plugin_route_secret_scope', lambda: None)
    lifecycles(app)

    def add(name, router=None, start=None, source='bundled'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'api.py').touch()
        router = router or APIRouter()
        router.add_api_route('/ping', lambda: {'ok': True})
        module = SimpleNamespace(router=router)
        if start is not None:
            module.start_legacy = start
        modules[name] = module
        plugins.append(dict(name=name, source=source, _dir=str(folder), _api_file='api.py'))
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


def _broken_lifespan_app(mount_env, close):
    """One plugin whose router lifespan raises on entry beside a healthy one, behind the warm gate."""
    app, dashboard, add = mount_env
    calls = []

    @asynccontextmanager
    async def broken(app):
        calls.append('enter')
        raise RuntimeError('broken enter')
        yield

    def start():
        calls.append('start')
        return SimpleNamespace(close=lambda: close(calls))

    add('broken', APIRouter(lifespan=broken), start=start)
    add('good')
    dashboard._register_plugin_api_fallback(app)
    app.add_api_route('/{full_path:path}', lambda: {'shell': True})
    app.state.plugin_api_sweep_owed = True
    app.state.http_listener_ready = asyncio.Event()
    app.state.sessions_facts = object()
    app.add_middleware(boot.BootServicesMiddleware)
    return app, dashboard, calls


@asynccontextmanager
async def _no_services(app):
    yield


@pytest.mark.asyncio
async def test_failed_router_lifespan_is_not_published_by_a_later_request(mount_env):
    app, dashboard, calls = _broken_lifespan_app(mount_env, lambda calls: calls.append('close'))
    async with boot.services_lifespan(app, _no_services):
        app.state.http_listener_ready.set()
        await asyncio.wait_for(app.state.boot_services_ready.wait(), 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            for _ in range(2):
                # The same answer the fallback gives any plugin API that is not mounted.
                response = await client.get('/api/plugins/broken/ping')
                assert (response.status_code, response.json()) == (404, {'detail': 'Not Found'})
                assert not dashboard._plugin_api_mounted(app, 'broken')
            assert (await client.get('/api/plugins/good/ping')).status_code == 200
    # No second import/start: the handle was stopped once and the lifespan was never re-entered.
    assert calls == ['start', 'enter', 'close']


@pytest.mark.asyncio
async def test_failing_close_of_a_refused_plugin_does_not_fail_warmup(mount_env, caplog):
    loop_thread = threading.get_ident()

    def close(calls):
        calls.append('close' if threading.get_ident() != loop_thread else 'close-on-loop')
        raise RuntimeError('broken close')

    app, dashboard, calls = _broken_lifespan_app(mount_env, close)
    async with boot.services_lifespan(app, _no_services):
        app.state.http_listener_ready.set()
        await asyncio.wait_for(app.state.boot_services_ready.wait(), 3)
        assert dashboard._plugin_api_mounted(app, 'good')
        assert not dashboard._plugin_api_mounted(app, 'broken')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            assert (await client.get('/api/plugins/good/ping')).status_code == 200
            assert (await client.get('/api/plugins/broken/ping')).status_code == 404
    assert calls == ['start', 'enter', 'close']
    assert 'Failed to stop plugin broken after its router lifespan failed' in caplog.text


@pytest.mark.asyncio
async def test_hosted_proxy_route_is_withheld_until_services_enter(mount_env, monkeypatch):
    from hermes_cli import plugin_isolation
    app, dashboard, add = mount_env
    add('hosted', source='user')
    monkeypatch.setattr(plugin_isolation, 'isolation_mode', lambda: plugin_isolation.ISOLATION_HOST)
    monkeypatch.setattr(dashboard, '_mount_hosted_plugin_api', lambda app, plugin, api_file: app.add_api_route(
        f"/api/plugins/{plugin['name']}/{{path:path}}", lambda path: {'hosted': path}))
    dashboard._register_plugin_api_fallback(app)
    app.add_api_route('/{full_path:path}', lambda: {'shell': True})
    app.state.plugin_api_sweep_owed = True
    seen = []

    @asynccontextmanager
    async def services(app):
        seen.append(dashboard._plugin_api_mounted(app, 'hosted'))
        yield

    async with boot.services_lifespan(app, services):
        assert seen == [False]
        assert [r.path for r in app.routes][4:] == [
            '/api/plugins/hosted/{path:path}', '/api/plugins/{name}/{path:path}', '/{full_path:path}']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            assert (await client.get('/api/plugins/hosted/x')).json() == {'hosted': 'x'}


def test_router_lifespan_state_reaches_requests_as_on_the_eager_path(mount_env):
    from fastapi import Request
    from fastapi.testclient import TestClient
    from hermes_cli import web_server as server
    _, dashboard, add = mount_env

    @asynccontextmanager
    async def stateful(app):
        yield {'probe_value': 7}

    def read(request: Request):
        return {'v': getattr(request.state, 'probe_value', None)}

    router = APIRouter(lifespan=stateful)
    router.add_api_route('/state', read)
    add('probe', router)
    plugin, = server._get_dashboard_plugins()
    answers = {}
    for mode in ('eager', 'deferred'):
        app = FastAPI(lifespan=lambda app: boot.services_lifespan(app, _no_services))
        app.add_middleware(boot.BootServicesMiddleware)
        if mode == 'eager':
            with dashboard._plugin_api_mount_lock:
                assert dashboard._mount_one_plugin_api(app, plugin, {'probe'}, set())
        else:
            app.state.plugin_api_sweep_owed = True
        dashboard._register_plugin_api_fallback(app)
        with TestClient(app) as client:
            response = client.get('/api/plugins/probe/state')
            answers[mode] = (response.status_code, response.json())
        assert not getattr(app.state, 'plugin_lifespan_state', None)  # nothing outlives the lifespans
    assert answers == {'eager': (200, {'v': 7}), 'deferred': (200, {'v': 7})}


def test_run_server_logs_when_deferral_does_not_engage(monkeypatch, caplog):
    from hermes_cli.dashboard_server_start import run_server
    from hermes_cli.observability import shared_metrics_process
    monkeypatch.setattr(boot, 'defer_plugin_api_sweep', lambda: False)
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_server', SimpleNamespace(start_server=lambda **kw: None))
    monkeypatch.setattr(shared_metrics_process, 'begin_process', lambda *args: None)
    with caplog.at_level('WARNING'):
        run_server(SimpleNamespace(host='localhost', port=1, no_open=True), headless=True,
                   owner=None, prepare_auth=lambda args: None, bootstrap={}, mcp_after_bind=None)
    assert 'Plugin API sweep was not deferred' in caplog.text
