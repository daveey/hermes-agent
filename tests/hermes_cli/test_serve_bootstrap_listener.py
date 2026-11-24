"""The bootstrap socket is already serving while application assembly is blocked."""
import http.client
import socket
import asyncio
from contextlib import asynccontextmanager

import pytest


def request(port, path, host='127.0.0.1'):
    client = http.client.HTTPConnection(host, port, timeout=2)
    try:
        client.request('GET', path)
        response = client.getresponse()
        return response.status, response.read()
    finally:
        client.close()


def test_listener_health_and_closed_application_gate_before_assembly():
    from hermes_cli.serve_bootstrap_listener import BootstrapListener

    with BootstrapListener('127.0.0.1', 0) as listener:
        # Legacy Desktop probes accept any 2xx: starting health must be non-2xx.
        status, body = request(listener.port, '/api/health')
        assert status == 503 and b'"ready":false' in body
        status, body = request(listener.port, '/api/profiles/sessions')
        assert status == 503
        assert b'sessions' not in body
        assert b'authentication' in body
        owned = listener.handoff()
        assert owned.getsockname()[1] == listener.port
        # The listening socket remains reserved across the ownership transfer.
        other = socket.socket()
        try:
            try:
                other.bind(('127.0.0.1', listener.port))
            except OSError:
                pass
            else:
                raise AssertionError('bootstrap listener released its port')
        finally:
            other.close()
    assert owned.fileno() == -1


@pytest.mark.asyncio
@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
async def test_handoff_keeps_answering_until_real_asgi_lifespan_is_ready(host):
    import uvicorn
    from fastapi import FastAPI
    from hermes_cli.serve_bootstrap_listener import BootstrapListener

    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        entered.set()
        await release.wait()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get('/api/health')
    def health():
        return {'ok': True}

    with BootstrapListener(host, 0) as listener:
        config = uvicorn.Config(app, log_config=None)
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)
        startup = asyncio.create_task(listener.startup(server))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            status, body = await asyncio.to_thread(request, listener.port, '/api/health')
            assert status == 503 and b'false' in body
            assert not listener.stopped
            release.set()
            await asyncio.wait_for(startup, 2)
            assert listener.stopped
            for bound in listener.servers:
                status, body = await asyncio.to_thread(
                    request, listener.port, '/api/health', bound.server_address[0])
                assert status == 200 and b'"ok":true' in body
        finally:
            release.set()
            await startup
            await server.shutdown()


def test_capture_starts_before_runtime_and_failed_assembly_releases_socket(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from hermes_cli import dashboard_server_start, sessions_early_boot

    order, ports = [], []
    from hermes_cli import serve_bootstrap_listener
    real_listener = serve_bootstrap_listener.BootstrapListener

    def listener(host, port):
        owned = real_listener(host, port)
        ports.append(owned.port)
        return owned

    @contextmanager
    def capture(**kwargs):
        assert request(ports[0], '/api/health')[0] == 503
        order.append('capture-start')
        try:
            yield object()
        finally:
            order.append('capture-close')

    def runtime(*args):
        assert request(ports[0], '/api/profiles/sessions')[0] == 503
        order.append('runtime')
        raise RuntimeError('assembly failed')

    monkeypatch.setattr(serve_bootstrap_listener, 'BootstrapListener', listener)
    monkeypatch.setattr(sessions_early_boot, 'early_sessions', capture)
    with pytest.raises(RuntimeError, match='assembly failed'):
        dashboard_server_start.prepare_and_run_server(
            SimpleNamespace(host='127.0.0.1', port=0), headless=True,
            prepare_runtime=runtime, prepare_auth=lambda args: pytest.fail('auth reached'), bootstrap={})
    assert order == ['capture-start', 'runtime', 'capture-close']
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', ports[0]))
