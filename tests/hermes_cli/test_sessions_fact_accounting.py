"""Facts-mode sessions serving stays visible to loop accounting. [daveey]

In facts mode the mounted route answers from owner memory and never calls the legacy
reader, so the legacy ``route/profiles.sessions*`` spans stop. Without a span of its own a
facts-served route reads as zero requests and zero CPU, which is how a canary's cost went
unseen. These tests drive the real mounted routes in both modes.
"""
import asyncio
import inspect

import httpx
import pytest
from fastapi import HTTPException

from hermes_cli import loop_accounting as la
from hermes_cli.fact_contract import Ready
from hermes_cli.sessions_fact_routes import ScopedSessionsRoutes, selector_for, use
from hermes_cli.web_routers import profiles
from tests.hermes_cli import test_sessions_fact_scope as scope_helpers
from tests.hermes_cli import test_sessions_native_startup as native_helpers
from tests.waiting import wait_until

native = native_helpers.native
profiles_on_disk = native_helpers.profiles_on_disk


def stat(name):
    return la.counters().get(name, {})


def runs(name):
    return stat(name).get('runs', 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('path,params,name,reader,legacy', [
    ('/api/profiles/sessions', {'limit': 2, 'profile': 'worker'}, 'sessions',
     'get_profiles_sessions', 'route/profiles.sessions'),
    ('/api/profiles/sessions/sidebar', {'recents_profile': 'worker'}, 'sessions_sidebar',
     'get_profiles_sessions_sidebar', 'route/profiles.sessions.sidebar'),
])
async def test_mounted_route_emits_a_span_in_each_mode(native, path, params, name, reader, legacy):
    selector = selector_for(inspect.signature(getattr(profiles, reader)), (), params)
    native.demand(native.scope, name, selector)

    def ready():
        return isinstance(native.store.snapshot(native.scope).view(name, selector), Ready)
    assert await asyncio.to_thread(wait_until, ready, timeout=10)
    facts = f'route/profiles.{name}.facts'
    transport = httpx.ASGITransport(app=native_helpers.app_for(native))
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        before = runs(facts), runs(legacy)
        assert (await client.get(path, params=params)).status_code == 200
        # Served from owner memory: the facts span counts it, the legacy reader did not run.
        assert (runs(facts), runs(legacy)) == (before[0] + 1, before[1])
        assert stat(facts)['errors'] == 0

        native.lifecycle.transition('sessions', native.scope, 'legacy', 10)
        native.generation = 10
        native.binding.on_mode('legacy', 10)
        before = runs(facts), runs(legacy)
        assert (await client.get(path, params=params)).status_code == 200
        assert runs(facts) == before[0]
        assert runs(legacy) >= before[1] + 1


@pytest.mark.asyncio
async def test_unavailable_projection_is_a_fallback_not_an_error():
    router = ScopedSessionsRoutes()

    async def legacy():
        raise AssertionError('facts mode must not reach the legacy reader')

    endpoint = router.wrap('sessions_stamps', legacy)
    item = scope_helpers.installed(router, 'a')  # publishes only the sidebar view
    name = 'route/profiles.sessions_stamps.facts'
    before = stat(name)
    try:
        with use(item[2]):
            with pytest.raises(HTTPException) as error:
                await endpoint()
        assert error.value.status_code == 503
    finally:
        scope_helpers.close(item)
    after = stat(name)
    assert after['runs'] == before.get('runs', 0) + 1
    assert after['fallbacks'] == before.get('fallbacks', 0) + 1
    assert after['errors'] == before.get('errors', 0)
