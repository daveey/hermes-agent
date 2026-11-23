"""Trusted dashboard mount owns startup and shutdown, not imports. [daveey]"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from hermes_cli import web_server_dashboard as dashboard


def module(events):
    from fastapi import APIRouter
    router = APIRouter()
    router.add_api_route('/ping', lambda: 'pong')
    class Handle:
        def close(self):
            events.append('close')
    return SimpleNamespace(router=router, start_legacy=lambda: (events.append('legacy'), Handle())[1],
                           bind_facts=lambda ctx, **kw: (events.append((ctx, kw)), Handle())[1])


def mount(monkeypatch, tmp_path, app, mod, *, enabled=True):
    from hermes_cli import plugin_isolation
    path = tmp_path / 'plugin_api.py'
    path.touch()
    plugin = {'name': 'mountprobe', 'source': 'bundled', '_dir': str(tmp_path), '_api_file': path.name}
    monkeypatch.setattr(plugin_isolation, 'import_user_code', lambda *args: mod)
    monkeypatch.setattr(dashboard, '_plugin_api_mount_skip_reason', lambda *args: None if enabled else 'disabled')
    return dashboard._mount_one_plugin_api(app, plugin, set(), set())


def test_legacy_start_and_shutdown_are_mount_owned(monkeypatch, tmp_path):
    events, app = [], FastAPI()
    mod = module(events)
    import asyncio
    from hermes_cli.fact_plugin_mount import lifespan

    async def running():
        async with lifespan(app):
            assert mount(monkeypatch, tmp_path, app, mod)
            assert events == ['legacy']
            assert mount(monkeypatch, tmp_path, app, mod) is False
    asyncio.run(running())
    assert events == ['legacy', 'close']


def test_trust_rejection_never_starts_lifecycle(monkeypatch, tmp_path):
    events = []
    assert not mount(monkeypatch, tmp_path, FastAPI(), module(events), enabled=False)
    assert not events


def test_explicit_binding_uses_exact_api_identity_and_preserves_context(monkeypatch, tmp_path):
    from hermes_cli.fact_plugin_mount import lifecycles
    events, app = [], FastAPI()
    ctx = SimpleNamespace(facts=object())
    lifecycles(app).authorize(tmp_path / 'plugin_api.py', ctx, {'provider': 'memory-only'})
    assert mount(monkeypatch, tmp_path, app, module(events))
    assert events == [(ctx, {'provider': 'memory-only'})]


def test_failed_route_mount_closes_handle_and_does_not_retain_success(monkeypatch, tmp_path):
    events, app = [], FastAPI()
    monkeypatch.setattr(app, 'include_router', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('bad router')))
    assert not mount(monkeypatch, tmp_path, app, module(events))
    assert events == ['legacy', 'close']


def test_partial_route_mount_is_rolled_back_before_retry(monkeypatch, tmp_path):
    events, app = [], FastAPI()
    original = tuple(app.router.routes)
    real_include = app.include_router

    def partial(*args, **kwargs):
        real_include(*args, **kwargs)
        raise ValueError('failed after route append')

    monkeypatch.setattr(app, 'include_router', partial)
    assert not mount(monkeypatch, tmp_path, app, module(events))
    assert tuple(app.router.routes) == original
    monkeypatch.setattr(app, 'include_router', real_include)
    assert mount(monkeypatch, tmp_path, app, module(events))
    assert events == ['legacy', 'close', 'legacy']


def test_reentry_refuses_before_serving_closed_bindings(tmp_path):
    import asyncio
    from hermes_cli.fact_plugin_mount import lifecycles, lifespan
    app = FastAPI()
    lifecycles(app).authorize(tmp_path / 'plugin_api.py', object(), {})

    async def running():
        async with lifespan(app):
            pass
        with pytest.raises(RuntimeError, match='closed'):
            async with lifespan(app):
                pytest.fail('closed app served')
    asyncio.run(running())


def test_failed_close_retains_custody_and_blocks_replacement(tmp_path):
    from hermes_cli.fact_plugin_mount import lifecycles
    app, events = FastAPI(), []
    registry = lifecycles(app)
    path = tmp_path / 'plugin_api.py'
    class Handle:
        failed = True
        def close(self):
            events.append('close')
            if self.failed:
                raise TimeoutError('not joined')
    handle = Handle()
    mod = SimpleNamespace(start_legacy=lambda: handle)
    registry.start(path, mod)
    with pytest.raises(TimeoutError):
        registry.stop(path)
    with pytest.raises(RuntimeError, match='custody'):
        registry.start(path, mod)
    handle.failed = False
    registry.stop(path)
    registry.start(path, mod)
    registry.close()
    assert events == ['close', 'close', 'close']


def test_legacy_only_app_reentry_restarts_mounted_lifecycles(tmp_path):
    from fastapi.testclient import TestClient
    from hermes_cli.fact_plugin_mount import include_router, lifespan
    events = []
    app = FastAPI(lifespan=lifespan)
    include_router(app, module(events), tmp_path / 'plugin_api.py')
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get('/ping').status_code == 200
    assert events == ['legacy', 'close', 'legacy', 'close']


def test_failed_drain_cannot_be_restarted_by_reentry(tmp_path):
    import asyncio
    from hermes_cli.fact_plugin_mount import lifecycles, lifespan
    app = FastAPI()
    started = []
    class Handle:
        def close(self):
            raise TimeoutError('not joined')
    registry = lifecycles(app)
    registry.start(tmp_path / 'plugin_api.py', SimpleNamespace(
        start_legacy=lambda: (started.append(True), Handle())[1]))
    async def run():
        with pytest.raises(ExceptionGroup):
            async with lifespan(app):
                pass
        with pytest.raises(RuntimeError, match='custody'):
            async with lifespan(app):
                pytest.fail('failed drain served')
    asyncio.run(run())
    assert started == [True]
