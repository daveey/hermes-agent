"""Default isolated capture reaches mounted HTTP with real rows and activity."""
import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI

from hermes_cli.sessions_native_startup import SessionsMiddleware, lifespan
from hermes_cli.web_routers import profiles
from hermes_state import SessionDB
from tests.waiting import wait_until


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_default_lifespan_child_capture_serves_real_list_and_sidebar(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    (home / 'config.yaml').write_text('kanban:\n  sync:\n    enabled: false\n    gateway: capture-fixture\n')
    db = SessionDB(db_path=home / 'state.db')
    try:
        db.create_session('parent', source='cli')
        db.append_message('parent', role='user', content='real capture')
        db.create_session('child', source='subagent', parent_session_id='parent')
        db.touch_session_activity('child', time.time())
    finally:
        db.close()
    diagnostics = tmp_path / 'capture-stderr'
    from hermes_cli import sessions_capture_process as process
    original_spawn = process.spawn_owned

    def diagnostic_spawn(*args, **kwargs):
        with diagnostics.open('ab') as output:
            kwargs['stderr'] = output
            return original_spawn(*args, **kwargs)

    monkeypatch.setattr(process, 'spawn_owned', diagnostic_spawn)
    app = FastAPI()
    app.add_middleware(SessionsMiddleware)
    app.include_router(profiles.sessions_router)
    # No injected capture, parent-only roster, clock or annotation doubles.
    async with lifespan(app):
        runtime = app.state.sessions_facts
        assert await asyncio.to_thread(wait_until, lambda: runtime.ready, timeout=10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for endpoint in ('/api/profiles/sessions', '/api/profiles/sessions/sidebar'):
                response = None

                async def ready():
                    nonlocal response
                    response = await client.get(endpoint, params={'profile': 'default', 'recents_profile': 'default'})
                    assert response.status_code in (200, 503), response.text
                    if response.status_code == 503:
                        assert response.json() == {'detail': 'sessions projection unavailable'}
                    return response.status_code == 200

                from tests.waiting import async_wait_until
                assert await async_wait_until(ready, timeout=15), (endpoint, diagnostics.read_text())
                # This proves real child publication and HTTP composition, not
                # the separate unit-start five-second production acceptance.
                started = time.monotonic()
                response = await client.get(endpoint, params={'profile': 'default', 'recents_profile': 'default'})
                assert response.status_code == 200, (endpoint, response.text, diagnostics.read_text())
                assert time.monotonic() - started < 5
                result = response.json()
                rows = result['recents']['sessions'] if endpoint.endswith('sidebar') else result['sessions']
                parent = next(row for row in rows if row['id'] == 'parent')
                observations = [o for o in parent['execution_observations'] if o['source'] == 'subagent.activity']
                assert len(observations) == 1
                assert observations[0]['state'] == 'running'
                assert observations[0]['identity']['work_id'] == 'child'
        owner = runtime.capture_reader
    assert owner._child is None and owner._closed
    assert app.state.sessions_facts is None
