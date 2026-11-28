"""The roller's incarnation proof answers while standalone services warm.

run71 natural canary (2026-11-28 05:25Z): the successor served real list/sidebar rows directly at
+4.95 s, but /api/host/identity stayed 503 behind service warmup, so the roller's incarnation
binding (and with it the proxy switch) waited for warmup to end at about +9 s.
"""
import asyncio
import os
from types import SimpleNamespace
import httpx
import pytest


def _client(app, web_server, *, token=True):
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test', headers=headers)


@pytest.fixture
def warming_app(monkeypatch):
    from hermes_cli import web_server
    app = web_server.app
    monkeypatch.setattr(app.state, 'boot_services_ready', asyncio.Event(), raising=False)  # warming
    monkeypatch.setattr(web_server, '_SSH_OWNER_NONCE', 'nonce-warm')
    monkeypatch.setattr(web_server, '_SSH_RUNTIME_MARKER', None)
    monkeypatch.setattr(web_server, '_SSH_RUNTIME_PURELIB', None)
    return web_server, app


@pytest.mark.asyncio
async def test_identity_and_ownership_answer_during_warmup(warming_app):
    web_server, app = warming_app
    async with _client(app, web_server) as client:
        identity = await client.get('/api/host/identity')
        assert identity.status_code == 200
        assert identity.json()['pid'] == os.getpid()
        ownership = await client.get('/api/ssh/ownership')
        assert ownership.status_code == 200
        assert ownership.json()['sshOwnerNonce'] == 'nonce-warm'
        assert ownership.json()['runtimeIntact'] is True
        # Everything else stays honestly unavailable until warmup ends.
        warming = await client.get('/api/models')
        assert warming.status_code == 503 and warming.headers['retry-after'] == '1'
        assert (await client.post('/api/host/identity')).status_code == 503
        assert (await client.get('/api/host/identity/extra')).status_code == 503


@pytest.mark.asyncio
async def test_identity_during_warmup_still_requires_the_token(warming_app):
    web_server, app = warming_app
    async with _client(app, web_server, token=False) as client:
        for path in ('/api/host/identity', '/api/ssh/ownership'):
            response = await client.get(path)
            assert response.status_code == 401, (path, response.status_code)
            assert 'nonce-warm' not in response.text


@pytest.mark.asyncio
async def test_roller_incarnation_binds_while_services_warm(warming_app):
    """Compose the real roller incarnation proof with the real app's warmup gate."""
    from hermes_cli import always_on_sessions
    web_server, app = warming_app
    loop = asyncio.get_running_loop()
    clock = [0.0]

    async with _client(app, web_server) as client:
        def get(port, path, timeout):
            response = asyncio.run_coroutine_threadsafe(client.get(path), loop).result(5)
            try:
                body = response.json()
            except ValueError:
                body = None
            return response.status_code, 0.0, body

        def sleep(seconds):
            clock[0] += max(seconds, 0.05)

        roller = SimpleNamespace(get=get, sleep=sleep, clock=lambda: clock[0],
                                 unit_pid=lambda port, timeout=15: os.getpid(),
                                 _unit_state=lambda port, timeout=15: 'active')
        bound = await asyncio.to_thread(always_on_sessions.incarnation, roller, 9148, 2.0)
    assert bound == (os.getpid(), 'nonce-warm')
    assert not app.state.boot_services_ready.is_set()
