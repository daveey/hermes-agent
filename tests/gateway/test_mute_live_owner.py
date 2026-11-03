"""Desktop ⌘B (POST /api/sessions/{id}/mute) and outcome refresh work against a live gateway.

Both took the offline maintenance lock, which a running gateway always holds, so every ⌘B on a
gateway-served profile failed with "Exclusive maintenance refused" and the park never stuck. They now
write on the live owner's own store, like owner-backfill. A backend that is NOT composed with the
gateway (Desktop's SSH-isolated ``serve`` on a remote host) forwards the write to that owner.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest

from tests.gateway.test_normal_runtime_boot import control
from tests.child_log import ChildLog


def _seed(home: Path, sid: str) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / 'state.db')
    try:
        db.create_session(sid, source='desktop')
        db.append_message(sid, role='user', content='fix it')
        db.append_message(sid, role='assistant', content='Fixed and pushed; CI is green.')
    finally:
        db.close()


@contextmanager
def _live_gateway(tmp_path):
    """A real gateway owning a fresh home seeded with session ``chat``; yields (home, descriptor)."""
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    model = {'model': {'provider': 'custom', 'default': 'm', 'base_url': 'http://127.0.0.1:1/v1'},
             'auxiliary': {'title_generation': {'enabled': False}}}
    (home / 'config.yaml').write_text(json.dumps(model))
    _seed(home, 'chat')
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               PYTHONUNBUFFERED='1')
    with ChildLog(tmp_path / 'gateway.log') as log:
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline, descriptor = time.monotonic() + 60, {}
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, 'identify')
                    if descriptor.get('state') == 'ready':
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            assert descriptor.get('state') == 'ready', (descriptor, log.text())
            yield home, descriptor
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.mark.platforms('linux')
def test_mute_and_outcome_refresh_run_on_the_live_owner(tmp_path):
    with _live_gateway(tmp_path) as (home, descriptor):
        def headers():
            # A native grant ticket is single-use: mint one per request, as Desktop does.
            return {'X-Hermes-Gateway-Ticket': control(home, 'session-ticket', {
                'profile_id': str(home.resolve()), 'instance_id': descriptor['instance_id'],
                'purpose': 'native-http'})['ticket']}
        with httpx.Client(base_url=descriptor['api_origin'], trust_env=False, timeout=30) as client:
            muted = client.post('/api/sessions/chat/mute', json={'muted': True}, headers=headers())
            assert muted.status_code == 200, muted.text
            assert muted.json()['muted'] is True and muted.json()['outcome']['kind'] == 'muted'
            stored = client.get('/api/sessions/chat/outcome', headers=headers())
            assert stored.status_code == 200, stored.text
            assert stored.json()['outcome']['kind'] == 'muted'

            unmuted = client.post('/api/sessions/chat/mute', json={'muted': False}, headers=headers())
            assert unmuted.status_code == 200, unmuted.text
            assert unmuted.json()['outcome']['kind'] != 'muted'

            refreshed = client.get('/api/sessions/chat/outcome', params={'refresh': 1}, headers=headers())
            assert refreshed.status_code == 200, refreshed.text
            assert client.post('/api/sessions/nope/mute', json={'muted': True}, headers=headers()).status_code == 404


@pytest.mark.platforms('linux')
def test_uncomposed_backend_forwards_mute_to_the_live_owner(tmp_path, monkeypatch):
    """Desktop on a remote host talks to an SSH-isolated ``serve`` that has no gateway runner; its
    writes must reach the running gateway instead of losing the maintenance lock to it (409)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.sessions import manage_router

    with _live_gateway(tmp_path) as (home, _descriptor):
        import hermes_state

        # The serving backend's home is the gateway's; the hermetic conftest re-points the default DB.
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
        app = FastAPI()
        app.include_router(manage_router)
        assert getattr(app.state, 'gateway_runner', None) is None
        with TestClient(app) as client:
            muted = client.post('/api/sessions/chat/mute', json={'muted': True})
            assert muted.status_code == 200, muted.text
            assert muted.json()['muted'] is True and muted.json()['outcome']['kind'] == 'muted'
            assert client.get('/api/sessions/chat/outcome').json()['outcome']['kind'] == 'muted'

            unmuted = client.post('/api/sessions/chat/mute', json={'muted': False})
            assert unmuted.status_code == 200, unmuted.text
            assert unmuted.json()['outcome']['kind'] != 'muted'

            refreshed = client.get('/api/sessions/chat/outcome', params={'refresh': 1})
            assert refreshed.status_code == 200, refreshed.text
            import sqlite3
            with sqlite3.connect(home / 'state.db') as raw:  # a legacy unowned row for the owner to stamp
                raw.execute("UPDATE sessions SET profile_name = NULL WHERE id = 'chat'")
            backfill = client.post('/api/sessions/owner-backfill', json={})
            assert backfill.status_code == 200, backfill.text
            assert backfill.json()['stamped'] == 1, backfill.text
            assert client.post('/api/sessions/nope/mute', json={'muted': True}).status_code == 404


def _uncomposed_client(monkeypatch, home):
    import hermes_state
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.sessions import manage_router

    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    app = FastAPI()
    app.include_router(manage_router)
    return TestClient(app)


@pytest.mark.platforms('linux')
def test_owner_refusing_the_forward_answers_503_not_a_credential_rejection(tmp_path, monkeypatch):
    """A 401/403 from the owner refuses the forward, not Desktop's credentials (which it would rotate)."""
    from hermes_cli import gateway_client

    with _live_gateway(tmp_path) as (home, _descriptor):
        monkeypatch.setattr(gateway_client, '_session_ticket', lambda *a, **k: 'not-a-ticket')
        with _uncomposed_client(monkeypatch, home) as client:
            refused = client.post('/api/sessions/chat/mute', json={'muted': True})
            assert refused.status_code == 503, refused.text
            assert refused.json()['detail'] == 'owner_unreachable'


def test_no_gateway_keeps_the_offline_write(tmp_path, monkeypatch):
    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    _seed(home, 'chat')
    with _uncomposed_client(monkeypatch, home) as client:
        muted = client.post('/api/sessions/chat/mute', json={'muted': True})
        assert muted.status_code == 200, muted.text
        assert muted.json()['outcome']['kind'] == 'muted'


def test_standalone_profile_ticket_is_minted_on_the_profiles_own_socket(tmp_path, monkeypatch):
    """A profile gateway that answered on its own socket (no control_home) mints there, never on the
    serving backend's launch home, whose socket belongs to another owner or none."""
    from hermes_cli import gateway_client
    from hermes_cli import gateway_runtime
    from hermes_cli.gateway_runtime import GatewayDiscovery, GatewayEndpoint

    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    _seed(home, 'chat')
    endpoint = GatewayEndpoint(profile_id=str(home.resolve()), instance_id='i', authority_epoch=1,
                               runtime_protocol=1, api_origin='http://127.0.0.1:9', supervisor='none',
                               capabilities=frozenset(), control_home=None)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda *_a, **_k: GatewayDiscovery('ready', endpoint=endpoint))
    minted = []

    def ticket(_home, endpoint, **_kw):
        minted.append(endpoint.control_home)
        raise OSError('stop after the mint')
    monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
    with _uncomposed_client(monkeypatch, home) as client:
        assert client.post('/api/sessions/chat/mute', json={'muted': True}).status_code == 503
    assert minted == [str(home.resolve())]


def test_unreachable_owner_answers_503_with_discoverys_reason_not_a_bare_409(tmp_path, monkeypatch):
    """An owner discovery saw but couldn't reach (starting, draining) still holds the lock: the
    offline write loses, and mute and owner-backfill answer a retryable 503 naming why."""
    from contextlib import contextmanager

    import gateway.runtime_ownership as ownership
    from hermes_cli import gateway_runtime
    from hermes_cli.gateway_runtime import GatewayDiscovery

    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    _seed(home, 'chat')
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda *_a, **_k: GatewayDiscovery('starting', reason_code='runtime_starting'))

    @contextmanager
    def held(_homes, **_kw):
        raise ownership.OwnershipConflict('Exclusive maintenance refused')
        yield
    monkeypatch.setattr(ownership, 'exclusive_maintenance', held)
    with _uncomposed_client(monkeypatch, home) as client:
        for path, payload in (('/api/sessions/chat/mute', {'muted': True}), ('/api/sessions/owner-backfill', {})):
            refused = client.post(path, json=payload)
            assert refused.status_code == 503, (path, refused.text)
            assert refused.json()['detail'].startswith('owner_unavailable: runtime_starting'), refused.text


def test_multiplexer_served_profile_keeps_its_control_home_and_forwards_the_profile(tmp_path, monkeypatch):
    """A named profile served by the default multiplexer: the ticket is minted on the multiplexer's
    socket (the endpoint's control_home, untouched) and the forwarded body still names the profile."""
    import httpx as httpx_module

    from hermes_cli import gateway_client
    from hermes_cli import gateway_runtime
    from hermes_cli.gateway_runtime import GatewayDiscovery, GatewayEndpoint
    import hermes_cli.web_routers.sessions as routes

    home, root = tmp_path / 'profiles' / 'work', tmp_path / 'root'
    home.mkdir(parents=True, mode=0o700)
    root.mkdir(mode=0o700)
    monkeypatch.setattr(routes, '_cron_profile_home', lambda profile: (profile, str(home)))
    endpoint = GatewayEndpoint(profile_id=str(home.resolve()), instance_id='i', authority_epoch=1,
                               runtime_protocol=1, api_origin='http://127.0.0.1:9', supervisor='none',
                               capabilities=frozenset(), control_home=str(root))
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda *_a, **_k: GatewayDiscovery('ready', endpoint=endpoint))
    minted, sent = [], []
    monkeypatch.setattr(gateway_client, '_session_ticket',
                        lambda _home, endpoint, **_kw: minted.append(endpoint.control_home) or 'ticket')

    def owner(request):
        sent.append((str(request.url), request.headers['X-Hermes-Gateway-Ticket'], json.loads(request.content)))
        return httpx_module.Response(200, json={'ok': True, 'muted': True})
    real_client = httpx_module.AsyncClient
    monkeypatch.setattr(httpx_module, 'AsyncClient',
                        lambda **kw: real_client(transport=httpx_module.MockTransport(owner), **kw))
    with _uncomposed_client(monkeypatch, home) as client:
        forwarded = client.post('/api/sessions/chat/mute', json={'muted': True, 'profile': 'work'})
        assert forwarded.status_code == 200, forwarded.text
    assert minted == [str(root)]
    assert sent == [('http://127.0.0.1:9/api/sessions/chat/mute', 'ticket', {'muted': True, 'profile': 'work'})]


def test_named_profile_backend_names_its_profile_to_the_multiplexer(tmp_path, monkeypatch):
    """A backend serving named profile ``work`` beside the default multiplexer: Desktop omits
    ``profile``, so the forward names it, or the owner would write its own default store."""
    import httpx as httpx_module

    from hermes_cli import gateway_client
    from hermes_cli import gateway_runtime
    from hermes_cli.gateway_runtime import GatewayDiscovery, GatewayEndpoint
    import hermes_cli.web_routers.sessions as routes

    home, root = tmp_path / 'profiles' / 'work', tmp_path / 'root'
    home.mkdir(parents=True, mode=0o700)
    root.mkdir(mode=0o700)
    monkeypatch.setattr(routes, '_cron_profile_home', lambda profile: (profile, str(home if profile == 'work' else root)))
    monkeypatch.setattr(routes, '_cron_default_profile', lambda: 'work')
    endpoint = GatewayEndpoint(profile_id=str(home.resolve()), instance_id='i', authority_epoch=1,
                               runtime_protocol=1, api_origin='http://127.0.0.1:9', supervisor='none',
                               capabilities=frozenset(), control_home=str(root))
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint',
                        lambda *_a, **_k: GatewayDiscovery('ready', endpoint=endpoint))
    monkeypatch.setattr(gateway_client, '_session_ticket', lambda *_a, **_k: 'ticket')
    sent = []

    def owner(request):
        sent.append((request.method, request.url.path, dict(request.url.params),
                     json.loads(request.content) if request.content else None))
        return httpx_module.Response(200, json={'ok': True})
    real_client = httpx_module.AsyncClient
    monkeypatch.setattr(httpx_module, 'AsyncClient',
                        lambda **kw: real_client(transport=httpx_module.MockTransport(owner), **kw))
    with _uncomposed_client(monkeypatch, home) as client:
        assert client.post('/api/sessions/chat/mute', json={'muted': True}).status_code == 200
        assert client.post('/api/sessions/owner-backfill', json={}).status_code == 200
        assert client.get('/api/sessions/chat/outcome', params={'refresh': 1}).status_code == 200
    assert sent == [('POST', '/api/sessions/chat/mute', {}, {'muted': True, 'profile': 'work'}),
                    ('POST', '/api/sessions/owner-backfill', {}, {'profile': 'work'}),
                    ('GET', '/api/sessions/chat/outcome', {'refresh': '1', 'profile': 'work'}, None)]
