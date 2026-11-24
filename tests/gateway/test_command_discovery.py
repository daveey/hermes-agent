"""Discovery stays transport-independent and cannot select another profile."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from tests.gateway.test_normal_runtime_boot import control


async def _rpc_result(ws, rid):
    """The ``result`` of the JSON-RPC reply with id ``rid``, skipping other frames (20 s budget)."""
    async with asyncio.timeout(20):
        while True:
            reply = json.loads(await ws.recv())
            if reply.get('id') == rid:
                assert 'result' in reply, reply
                return reply['result']


async def _wait_for_legacy_services(client, *, timeout=40):
    from tests.waiting import async_wait_until

    async def ready():
        response = await client.get('/')
        if response.status_code == 503 and response.json() == {'detail': 'Backend services warming'}:
            return False
        response.raise_for_status()
        return True

    await async_wait_until(ready, timeout=timeout, interval=.1, what='legacy standalone services')


async def _legacy_ws_snapshot(root, home, env, requests):
    legacy_home = home.parent / 'legacy-state'
    legacy_home.mkdir(mode=0o700)
    shutil.copy2(home / 'config.yaml', legacy_home / 'config.yaml')
    for directory in ('skills', 'plugins'):
        shutil.copytree(home / directory, legacy_home / directory)
    legacy_env = {**env, 'HERMES_HOME': str(legacy_home),
                  'HERMES_DASHBOARD_SESSION_TOKEN': 'disposable-discovery-token'}
    log_path = home.parent / 'legacy-serve.log'
    # The child writes through an O_APPEND handle and the poll reads by path. A shared 'w+'
    # handle shares its file offset with the child, so a poll's seek(0) made the child's next
    # line overwrite the READY sentinel and the wait never saw it.
    with log_path.open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'hermes_cli.main', 'serve',  # noqa: ASYNC220 -- Popen returns immediately; the test drives the child through its handle
                                    '--host', '127.0.0.1', '--port', '0', '--skip-build', '--isolated'],
                                   cwd=root, env=legacy_env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            port = None
            output = ''
            while process.poll() is None and time.monotonic() < deadline:
                output = log_path.read_text(errors='replace')
                if match := re.search(r'HERMES_BACKEND_READY port=(\d+)', output):
                    port = int(match.group(1))
                    break
                await asyncio.sleep(.1)
            assert port, output
            # READY announces the listener; BootServicesMiddleware still closes RPC
            # sockets with 1013 until standalone services finish warming.
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=5, trust_env=False) as client:
                await _wait_for_legacy_services(client)
            results = []
            async with connect(f'ws://127.0.0.1:{port}/api/ws?token=disposable-discovery-token') as ws:
                for rid, (method, params) in enumerate(requests):
                    await ws.send(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}))
                    results.append(await _rpc_result(ws, rid))
            process.send_signal(signal.SIGINT)
            # Wait for actual exit, not a 20 s wall-clock performance deadline: CI can
            # deschedule the child during graceful shutdown. A hang is still bounded.
            assert process.wait(timeout=120) == 0
            return results, {'legacy_ws_pid': process.pid, 'legacy_ws_port': port,
                             'legacy_ws_exit': process.returncode}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.mark.platforms("linux")
def test_ordinary_daemon_discovery_matches_legacy_extensions(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'discovery-fixture',
                  'base_url': 'http://127.0.0.1:1/v1'},
        'auxiliary': {'title_generation': {'enabled': False}},
        'quick_commands': {'probe-quick': {'type': 'alias', 'target': '/help',
                                          'description': 'Disposable quick discovery'}},
        'plugins': {'enabled': ['probe_plugin']},
    }))
    skill = home / 'skills' / 'probe-skill'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: probe-skill\ndescription: Disposable skill discovery\n---\n# Probe\nOnly a discovery fixture.\n')
    plugin = home / 'plugins' / 'probe_plugin'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: probe_plugin\nversion: 0.1.0\ndescription: Discovery fixture\n')
    (plugin / '__init__.py').write_text(
        "def register(ctx):\n    ctx.register_command('probe-plugin', lambda args: args, description='Disposable plugin discovery')\n")
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               PYTHONPATH=str(root), PYTHONUNBUFFERED='1')
    requests = [('commands.catalog', {}), *[('complete.slash', {'text': text})
        for text in ('/', '/probe', '/probe-quick', '/help', '/details c', 'plain')]]
    legacy_code = '''import json, sys
from tui_gateway import server
requests = json.loads(sys.argv[1])
results = [server._methods[method](i, params)['result'] for i, (method, params) in enumerate(requests)]
sys.__stdout__.write(json.dumps(results) + '\\n')
'''

    async def probe(descriptor):
        ws_baseline, legacy_receipt = await _legacy_ws_snapshot(root, home, env, requests)
        assert ws_baseline == expected
        url = descriptor['api_origin'].replace('http:', 'ws:') + '/api/ws'
        with pytest.raises(InvalidStatus):
            async with connect(url):
                pass
        ticket = control(home, 'session-ticket', {
            'profile_id': str(home), 'instance_id': descriptor['instance_id'], 'purpose': 'interactive'})
        async with connect(url, subprotocols=['hermes-gateway-v1',
                           'hermes-gateway-ticket.' + ticket['ticket']]) as ws:
            async def rpc(method, params):
                await ws.send(json.dumps({'jsonrpc': '2.0', 'id': method, 'method': method, 'params': params}))
                async with asyncio.timeout(20):
                    while True:
                        reply = json.loads(await ws.recv())
                        if reply.get('id') == method:
                            return reply
            for (method, params), baseline in zip(requests, expected):
                reply = await rpc(method, params)
                got = reply.get('result')
                if got != baseline and isinstance(got, dict) and 'skills' in got:
                    a, b = got['skills'], baseline['skills']
                    delta = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
                    raise AssertionError(f'skills delta daemon vs legacy: {delta}')
                assert got == baseline, reply
            for method in ('commands.catalog', 'complete.slash'):
                reply = await rpc(method, {'profile': 'foreign-profile', **({'text': '/probe'} if method == 'complete.slash' else {})})
                assert reply['error']['message'] == 'profile_mismatch', reply
        return {**legacy_receipt, 'legacy_pid_exit': legacy.returncode, 'catalog_pairs': len(pairs),
                'skill_count': expected[0]['skill_count'], 'compared_requests': len(requests),
                'unauthenticated_ws': 'rejected', 'foreign_profile': 'rejected'}

    gateway_log = tmp_path / 'gateway.log'
    with gateway_log.open('ab') as log:  # O_APPEND, read by path: see _legacy_ws_snapshot
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            descriptor = {}
            deadline = time.monotonic() + 40
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, 'identify')
                    if descriptor.get('state') == 'ready':
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            assert descriptor.get('state') == 'ready', (descriptor, gateway_log.read_text(errors='replace'))
            # Ordinary startup syncs bundled skills; compare both transports on that same snapshot.
            legacy = subprocess.run([sys.executable, '-c', legacy_code, json.dumps(requests)],
                                    cwd=root, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=45, check=False)
            assert legacy.returncode == 0, legacy.stderr
            expected = json.loads(legacy.stdout.splitlines()[-1])
            pairs = dict(expected[0]['pairs'])
            for name in ('/probe-skill', '/probe-quick', '/probe-plugin'):
                assert name in pairs, (name, expected[0]['warning'])
            assert {'probe-skill', 'probe-plugin'} <= {
                row['text'].strip().lstrip('/') for row in expected[2]['items']}
            receipt = asyncio.run(probe(descriptor))
            process.send_signal(signal.SIGINT)
            # Wait for actual exit, not a 20 s wall-clock performance deadline: CI can
            # deschedule the child during graceful shutdown. A hang is still bounded.
            assert process.wait(timeout=120) == 0
            print(json.dumps({**receipt, 'pid': process.pid, 'daemon_exit': process.returncode,
                              'instance_id': descriptor['instance_id'], 'api_origin': descriptor['api_origin']}))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.mark.asyncio
async def test_legacy_readiness_waits_for_services_not_listener():
    from hermes_cli.web_boot_services import BootServicesMiddleware
    from starlette.responses import Response

    ready, warming_seen = asyncio.Event(), asyncio.Event()
    app = SimpleNamespace(state=SimpleNamespace(boot_services_ready=ready))
    paths = []

    async def serve(scope, receive, send):
        paths.append(scope['path'])
        await Response(status_code=204)(scope, receive, send)

    middleware = BootServicesMiddleware(serve)

    async def observed(scope, receive, send):
        scope['app'] = app
        async def observe_send(message):
            await send(message)
            if message['type'] == 'http.response.start' and message['status'] == 503:
                warming_seen.set()
        await middleware(scope, receive, observe_send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url='http://test') as client:
        # Health is intentionally available before standalone RPC services.
        assert (await client.get('/api/health')).status_code == 204
        waiter = asyncio.create_task(_wait_for_legacy_services(client))
        try:
            await asyncio.wait_for(warming_seen.wait(), 5)
            assert not waiter.done()
            assert paths == ['/api/health']
            ready.set()
            await asyncio.wait_for(waiter, 5)
            assert paths == ['/api/health', '/']
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(401, {}), (500, {}), (503, {'detail': 'unrelated'})])
async def test_legacy_readiness_preserves_non_warmup_errors(status, body):
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://test') as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _wait_for_legacy_services(client)
    assert calls == ['/']


@pytest.mark.asyncio
async def test_legacy_readiness_bounds_failed_warmup():
    def respond(request):
        return httpx.Response(503, json={'detail': 'Backend services warming'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://test') as client:
        with pytest.raises(AssertionError, match='legacy standalone services'):
            await _wait_for_legacy_services(client, timeout=0)


@pytest.mark.asyncio
async def test_discovery_authorizes_before_scanning(tmp_path, monkeypatch):
    from agent import skill_commands
    from hermes_cli import plugins
    from gateway.session_controls import AuthorityConnection

    def forbidden(*args, **kwargs):
        pytest.fail('unauthorized request reached discovery')
    monkeypatch.setattr(skill_commands, 'scan_skill_commands', forbidden)
    monkeypatch.setattr(plugins, 'get_plugin_commands', forbidden)
    authority = SimpleNamespace(instance_id='owned', profile_id=str(tmp_path), events={})
    for identity in ({}, {'user_id': 'reader', 'capabilities': []},
                     {'user_id': 'reader', 'profile_id': 'foreign'},
                     {'user_id': 'reader', 'instance_id': 'stale'}):
        connection = AuthorityConnection(authority, object(), identity)
        try:
            for method in ('commands.catalog', 'complete.slash'):
                reply = await connection.dispatch({'id': 1, 'method': method,
                    'params': {'text': '/probe'} if method == 'complete.slash' else {}})
                assert reply['error']['message'] == 'permission_denied', reply
        finally:
            await connection.close()
