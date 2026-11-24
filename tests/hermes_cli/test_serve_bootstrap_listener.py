"""The bootstrap socket is already serving while application assembly is blocked."""
import http.client
import socket
import asyncio
from contextlib import asynccontextmanager

import pytest


def request(port, path):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        client.request('GET', path)
        response = client.getresponse()
        return response.status, response.read()
    finally:
        client.close()


def test_listener_health_and_closed_application_gate_before_assembly():
    from hermes_cli.serve_bootstrap_listener import BootstrapListener

    with BootstrapListener('127.0.0.1', 0) as listener:
        assert request(listener.port, '/api/health')[0] == 200
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
async def test_handoff_keeps_answering_until_real_asgi_lifespan_is_ready():
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

    with BootstrapListener('127.0.0.1', 0) as listener:
        config = uvicorn.Config(app, log_config=None)
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)
        startup = asyncio.create_task(listener.startup(server))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            status, body = await asyncio.to_thread(request, listener.port, '/api/health')
            assert status == 200 and b'false' in body
            assert not listener.stopped
            release.set()
            await asyncio.wait_for(startup, 2)
            assert listener.stopped
            status, body = await asyncio.to_thread(request, listener.port, '/api/health')
            assert status == 200 and b'"ok":true' in body
        finally:
            release.set()
            await startup
            await server.shutdown()
