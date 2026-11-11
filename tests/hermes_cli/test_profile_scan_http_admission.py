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
    monkeypatch.setattr(profiles, 'profile_project_trees', projects)
    monkeypatch.setattr(profiles, '_profile_state_db', lambda home: db_path)
    monkeypatch.setattr(profiles, 'subagent_listing_scope', lambda *a, **kw: (False, None))
    monkeypatch.setattr(profiles, '_sidebar_profile_cache_get', lambda key: None)
    monkeypatch.setattr(profiles, 'automated_session_sources', lambda home: set())
    monkeypatch.setattr(profiles, '_profile_targets', lambda label: [('default', None)])
    monkeypatch.setattr(profiles, '_read_profile_db', read)
    monkeypatch.setattr(profiles, '_corrupt_profile_stores', lambda targets: {})
    for name in ('kanban', 'goal', 'cron', 'background', 'turn'):
        monkeypatch.setattr(profiles, f'annotate_{name}_activity', lambda rows: rows)
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
            cached_route = param != 'limit'
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
                assert limiter.borrowed_tokens == 1
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
