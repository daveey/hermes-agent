"""Profile scan contenders must queue before occupying shared HTTP workers."""
import asyncio
import threading

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('route,param', [
    ('/api/profiles/sessions', 'limit'),
    ('/api/profiles/sessions/sidebar', 'recents_limit'),
    ('/api/profiles/projects/tree', 'preview_limit'),
    ('/api/sessions', 'limit'),
])
async def test_profile_scan_burst_keeps_status_responsive(monkeypatch, tmp_path, route, param):
    import anyio.to_thread
    import httpx
    from fastapi import FastAPI
    from hermes_cli.web_routers import profiles

    release = threading.Event()
    entered = threading.Event()
    calls = []

    def read(name, home, errors, callback):
        calls.append(name)
        entered.set()
        assert release.wait(5)
        if route.endswith('/sidebar'):
            return {'recents': [], 'cron': [], 'messaging': [], 'usage': {}}
        return []

    from hermes_cli.web_routers import sessions

    def projects(*args, **kwargs):
        read('default', None, [], None)
        return []

    def payload(*args):
        if args[-1] != 'other':
            read('default', None, [], None)
        return {'sessions': [], 'limit': args[0]}

    db_path = tmp_path / 'state.db'
    db_path.touch()
    monkeypatch.setattr(sessions, '_session_db_path_for_profile', lambda profile: db_path)
    monkeypatch.setattr(sessions, '_list_sessions_payload', payload)
    monkeypatch.setattr(sessions, 'data_stamp', lambda path: ('stable',))
    sessions.SESSION_LIST_CACHE.clear()
    monkeypatch.setattr(profiles, 'profile_project_trees', projects)
    monkeypatch.setattr(profiles, '_profile_state_db', lambda home: db_path)
    monkeypatch.setattr(profiles, 'subagent_listing_scope', lambda *a, **kw: (False, None))
    monkeypatch.setattr(profiles, '_sidebar_profile_cache_get', lambda key: None)
    monkeypatch.setattr(profiles, 'automated_session_sources', lambda home: set())
    monkeypatch.setattr(profiles, '_profile_targets', lambda label: [('default', None)])
    monkeypatch.setattr(profiles, '_read_profile_db', read)
    monkeypatch.setattr(profiles, '_corrupt_profile_stores', lambda targets: {})
    for name in ('kanban', 'goal', 'cron', 'background', 'turn'):
        monkeypatch.setattr(profiles, f'annotate_{name}_activity', lambda rows, **kw: rows)
    monkeypatch.setattr(profiles, 'annotate_review_wait', lambda rows: rows)
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    app.include_router(sessions.list_router)

    @app.get('/api/status')
    def status():
        return {'ok': True}

    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = 2
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            cached_route = param != 'limit' or route == '/api/sessions'
            if cached_route:
                release.set()
                warm = await client.get(route, params={param: 99})
                assert warm.status_code == 200
                release.clear()
                entered.clear()
                calls.clear()
            tasks = [asyncio.create_task(client.get(route, params={param: n}))
                     for n in range(1, 13)]
            try:
                from tests.waiting import async_wait_until
                await async_wait_until(entered.is_set, timeout=3)
                for _ in range(50):
                    await asyncio.sleep(0)
                response = await asyncio.wait_for(client.get('/api/status'), 0.5)
                assert response.status_code == 200
                assert response.json() == {'ok': True}
                # Cache peeks for other /api/sessions requests use the shared pool
                # before lane admission; status completion does not drain them.
                # A scan wrongly using that pool cannot drain while release is unset.
                await async_wait_until(lambda: limiter.borrowed_tokens == 0, timeout=3)
                from hermes_cli import web_thread_lanes
                assert limiter.borrowed_tokens == 0
                assert web_thread_lanes.stats()["sessions"]["in_use"] == 1
                if route == '/api/sessions':
                    other = await asyncio.wait_for(client.get(route, params={'profile': 'other'}), 0.5)
                    assert other.status_code == 200
                if cached_route:
                    hit = await asyncio.wait_for(client.get(route, params={param: 99}), 0.5)
                    assert hit.json() == warm.json()
                    assert len(calls) == 1
            finally:
                release.set()
                responses = await asyncio.gather(*tasks)
            assert [r.status_code for r in responses] == [200] * 12
            if param == 'limit':
                assert [r.json()['limit'] for r in responses] == list(range(1, 13))
            assert len(calls) == 12
            if param == 'limit':
                cap = 501 if route.startswith('/api/profiles') else 101
                assert (await client.get(route, params={'limit': cap})).status_code == 422
            assert (await client.get('/api/profiles/sessions?archived=bogus')).status_code == 400
    finally:
        limiter.total_tokens = previous
        sessions.SESSION_LIST_CACHE.clear()


@pytest.mark.asyncio
async def test_session_peek_can_outlive_status_response(monkeypatch, tmp_path):
    """A completed status response is not a barrier for other requests' peeks."""
    import httpx
    from hermes_cli.web_routers import sessions
    from tests.waiting import async_wait_until

    peek_entered = threading.Event()
    release_peek = threading.Event()
    original_peek = sessions._peek_sessions_encoded
    original_get = httpx.AsyncClient.get
    original_json = httpx.Response.json

    def delayed_peek(arguments):
        if arguments['limit'] == 12:
            peek_entered.set()
            assert release_peek.wait(5)
        return original_peek(arguments)

    async def get(client, url, *args, **kwargs):
        if url == '/api/status':
            await async_wait_until(peek_entered.is_set, timeout=3)
        return await original_get(client, url, *args, **kwargs)

    def response_json(response, *args, **kwargs):
        value = original_json(response, *args, **kwargs)
        if response.request.url.path == '/api/status':
            # Keep the real shared token through JSON validation. Release on the
            # next loop turn, not before the original instantaneous assertion.
            asyncio.get_running_loop().call_soon(release_peek.set)
        return value

    monkeypatch.setattr(sessions, '_peek_sessions_encoded', delayed_peek)
    monkeypatch.setattr(httpx.AsyncClient, 'get', get)
    monkeypatch.setattr(httpx.Response, 'json', response_json)
    try:
        await test_profile_scan_burst_keeps_status_responsive(
            monkeypatch, tmp_path, '/api/sessions', 'limit')
    finally:
        release_peek.set()


def test_session_list_peek_requires_fresh_matching_stamp():
    from hermes_cli.session_list_flight import SingleFlightCache, copy_session_list
    now = [0.0]
    cache = SingleFlightCache(clock=lambda: now[0], copy=copy_session_list)
    assert cache.peek('missing', ('a',)) == (False, None)
    cache.get('key', ('a',), lambda: {'sessions': [{'id': 'first'}]})
    found, value = cache.peek('key', ('a',))
    assert found
    value['sessions'][0]['id'] = 'changed'
    assert cache.peek('key', ('a',))[1]['sessions'][0]['id'] == 'first'
    assert cache.peek('key', ('b',)) == (False, None)
    assert cache.peek('key', None) == (False, None)
    cache.ttl_s = 0
    assert cache.peek('key', ('a',)) == (False, None)
    cache.ttl_s = 2
    now[0] = 2.0
    assert cache.peek('key', ('a',)) == (False, None)

