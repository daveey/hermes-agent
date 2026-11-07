"""Public polls avoid peer lookup; authenticated verbs recheck every connection."""
import asyncio
import json
import os
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.control_socket import GatewayControlServer


async def exchange(server, request, sock=None):
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps(request).encode() + b'\n')
    reader.feed_eof()
    output = []

    async def drain():
        pass

    writer = SimpleNamespace(write=output.append, drain=drain, close=lambda: None,
                             get_extra_info=lambda key: sock)
    from gateway.control_dispatch import ControlRequestDispatcher
    server._request_dispatch = ControlRequestDispatcher()
    try:
        await server._handle_connection(reader, writer)
    finally:
        server._request_dispatch.close()
        server._request_dispatch = None
    assert len(output) == 1
    return json.loads(output[0])


@pytest.mark.parametrize('verb', ['identify', 'status', 'unknown'])
def test_public_connection_never_looks_up_peer(tmp_path, monkeypatch, verb):
    server = GatewayControlServer(tmp_path, verb_handlers={
        'identify': lambda: {'alive': True}, 'status': lambda: {'alive': True}})
    lookup = Mock(return_value=None)
    monkeypatch.setattr(server, '_posix_peer_subject', lookup)
    reply = asyncio.run(exchange(server, {'verb': verb}))
    assert reply['ok'] is (verb != 'unknown')
    lookup.assert_not_called()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('verb', ['private', 'identify', 'session-ticket'])
def test_private_connection_rechecks_home_and_peer(tmp_path, monkeypatch, verb):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    server = GatewayControlServer(home, verb_handlers={'identify': lambda: {'public': True}})
    private = Mock(return_value={'private': True})
    if verb != 'session-ticket':
        server.private_handlers[verb] = private
    mint = Mock(return_value='ticket')
    server.ticket_store = SimpleNamespace(instance_id='instance', mint=mint)
    request = {'protocol': 1, 'verb': verb, 'params': {
        'profile_id': 'profile', 'instance_id': 'instance', 'purpose': 'attach'}}
    from hermes_cli import gateway_runtime_discovery as discovery
    ours, theirs = socket.socketpair()
    try:
        assert asyncio.run(exchange(server, request, ours))['ok'] is True
        called = mint if verb == 'session-ticket' else private
        assert called.call_count == 1
        if verb != 'session-ticket':
            assert private.call_args.args[1] == f'uid:{os.getuid()}'
        else:
            assert mint.call_args.kwargs['subject'] == f'uid:{os.getuid()}'
        home.chmod(0o777)
        assert asyncio.run(exchange(server, request, ours))['ok'] is False
        home.chmod(0o700)
        monkeypatch.setattr(discovery, 'socket_peer_uid', lambda sock: os.getuid() + 1)
        assert asyncio.run(exchange(server, request, ours))['ok'] is False
        assert called.call_count == 1
    finally:
        ours.close()
        theirs.close()
