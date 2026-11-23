"""Serve cutover uses an in-process publication, never the legacy queue."""
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_session_activity as activity
from tests.waiting import wait_until


@pytest.fixture
def origin(monkeypatch):
    from hermes_cli.kanban_links_refresh import ScopeRefresher
    scope = ('test-home', 'default', 'boards', 'hub', 'mail')
    monkeypatch.setattr(activity, '_origin_scope', lambda: scope)
    admissions = []
    monkeypatch.setattr(activity, '_links_refresher', ScopeRefresher(admissions.append, 16))
    monkeypatch.setattr(activity, '_project_remote_links', lambda: [{'id': 't_one', 'status': 'running'}])
    return scope, admissions


def test_serve_lifespan_defaults_on_and_reads_publication(origin, monkeypatch):
    from hermes_cli.fact_links_startup import lifespan, binding_for
    scope, admissions = origin
    monkeypatch.delenv('HERMES_FACTS_LINKS', raising=False)
    app = FastAPI(lifespan=lifespan)

    @app.get('/')
    def display():
        return activity._remote_links(stale_ok=True)

    with TestClient(app) as client:
        binding = binding_for(scope)
        assert binding is not None
        wait_until(lambda: binding.handle.v1.snapshot(binding.scope).get(binding.key) is not None)
        expected = activity._project_remote_links()
        import time
        from hermes_cli.fact_remote_links import RemoteLinksSource
        def recapture_scheduled():
            with binding.handle.owner.feed._condition:
                return any(isinstance(s.adapter, RemoteLinksSource) and s.due is not None
                           and 25 < s.due - time.monotonic() <= 30
                           for s in binding.handle.owner.feed._sources)
        wait_until(recapture_scheduled)
        monkeypatch.setattr(activity, '_links_lookup', lambda *a, **k: pytest.fail('legacy display read'))
        for _ in range(50):
            activity._refresh_links_in_background(scope)
            assert client.get('/').json() == expected
        assert admissions == [] and not activity._links_refresher.busy
        assert activity.remote_links_and_origin_rows(stale_ok=True) == (expected, [])
        # Caller mutation cannot modify the shared generation.
        activity._remote_links(stale_ok=True)[0]['status'] = 'corrupt'
        assert client.get('/').json() == expected
    assert binding_for(scope) is None
    activity._refresh_links_in_background(scope)
    wait_until(lambda: admissions == [scope])


def test_switch_off_resumes_and_preserves_strict_and_display_paths(origin, monkeypatch):
    from hermes_cli.fact_links_startup import lifespan, binding_for
    scope, admissions = origin
    monkeypatch.setenv('HERMES_FACTS_LINKS', '0')
    activity._links_refresher.drain(scope, timeout=0)
    calls = []
    monkeypatch.setattr(activity, '_links_lookup', lambda scope, stale: calls.append(stale) or (0, scope, (), ['legacy'], ['row']))
    with TestClient(FastAPI(lifespan=lifespan)):
        assert binding_for(scope) is None
        assert activity._remote_links(stale_ok=True) == ['legacy']
        assert activity._remote_links() == ['legacy']
        activity._refresh_links_in_background(scope)
        wait_until(lambda: admissions == [scope])
    assert calls == [True, False]


def test_bound_strict_and_foreign_scopes_remain_legacy(origin, monkeypatch):
    from hermes_cli.fact_links_startup import lifespan
    monkeypatch.delenv('HERMES_FACTS_LINKS', raising=False)
    monkeypatch.setattr(activity, '_links_lookup', lambda scope, stale: (0, scope, (), ['strict'], []))
    with TestClient(FastAPI(lifespan=lifespan)):
        assert activity._remote_links() == ['strict']
        monkeypatch.setattr(activity, '_origin_scope', lambda: ('foreign',))
        assert activity._remote_links(stale_ok=True) == ['strict']


def test_drain_timeout_keeps_fence_and_does_not_start_replacement(origin, monkeypatch):
    from hermes_cli.fact_links_startup import LinksRuntime, binding_for
    scope, _ = origin
    started, release = threading.Event(), threading.Event()
    def blocked(_):
        started.set()
        assert release.wait(5)
    activity._links_refresher._refresh = blocked
    activity._refresh_links_in_background(scope)
    assert started.wait(5)
    runtime = LinksRuntime(scope)
    try:
        with pytest.raises(TimeoutError):
            runtime.start(timeout=0)
        assert runtime.owner is None and binding_for(scope) is None
        assert scope in activity._links_refresher._fenced
    finally:
        release.set()
        wait_until(lambda: not activity._links_refresher.busy)
        runtime.close()
    assert scope not in activity._links_refresher._fenced


def test_real_app_composition_binds_before_services_and_releases_after(origin, monkeypatch):
    from contextlib import asynccontextmanager
    from hermes_cli import web_server_app
    from hermes_cli.fact_links_startup import binding_for
    scope, _ = origin
    monkeypatch.delenv('HERMES_FACTS_LINKS', raising=False)
    seen = []

    @asynccontextmanager
    async def services(app):
        seen.append(binding_for(scope) is not None)
        yield
        seen.append(binding_for(scope) is not None)

    monkeypatch.setattr(web_server_app, 'standalone_lifespan', services)
    with TestClient(FastAPI(lifespan=web_server_app.app_lifespan)):
        assert binding_for(scope) is not None
    assert seen == [True, True] and binding_for(scope) is None


def test_failed_cleanup_retains_fence_binding_and_blocks_duplicate(origin, monkeypatch):
    from hermes_cli.fact_links_startup import LinksRuntime, binding_for
    scope, _ = origin
    runtime = LinksRuntime(scope).start()
    close = runtime.owner.close
    def failed():
        raise TimeoutError('owned capture still running')
    monkeypatch.setattr(runtime.owner, 'close', failed)
    try:
        with pytest.raises(TimeoutError):
            runtime.close()
        assert binding_for(scope) is runtime.binding
        assert scope in activity._links_refresher._fenced
        with pytest.raises(RuntimeError, match='custody'):
            LinksRuntime(scope).start()
    finally:
        monkeypatch.setattr(runtime.owner, 'close', close)
        runtime.close()
    assert binding_for(scope) is None
    assert scope not in activity._links_refresher._fenced
