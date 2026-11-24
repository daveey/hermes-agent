"""Bootstrap binds preserve asyncio's address-family exposure and fallback."""
import errno
import socket

import pytest

from hermes_cli import serve_bootstrap_listener as bootstrap
from tests.hermes_cli.test_serve_bootstrap_listener import request


def ipv6_loopback_available():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(('::1', 0))
    except OSError:
        return False
    return True


def test_ipv6_listener_never_accepts_ipv4():
    if not ipv6_loopback_available():
        pytest.skip('IPv6 loopback unavailable')
    with bootstrap.BootstrapListener('::', 0) as listener:
        assert request(listener.port, '/api/health', '::1')[0] == 503
        assert listener.server.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
        with pytest.raises(OSError):
            socket.create_connection(('127.0.0.1', listener.port), timeout=2)
        listener.handoff()
        # This same socket is handed to ASGI; its exposure cannot change.
        assert listener.server.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1


@pytest.mark.parametrize('unavailable_first', [True, False])
def test_hostname_skips_unavailable_resolved_address(monkeypatch, unavailable_first):
    unavailable = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 0))
    available = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0))
    addresses = [unavailable, available] if unavailable_first else [available, unavailable]
    resolve = bootstrap.socket.getaddrinfo
    monkeypatch.setattr(bootstrap.socket, 'getaddrinfo', lambda *args, **kwargs: addresses)
    with bootstrap.BootstrapListener('localhost', 0) as listener:
        monkeypatch.setattr(bootstrap.socket, 'getaddrinfo', resolve)
        assert len(listener.servers) == 1
        assert request(listener.port, '/api/health')[0] == 503
        owned = listener.handoff()
        assert owned.fileno() >= 0
    assert owned.fileno() == -1


def test_no_available_address_raises_oserror(monkeypatch):
    monkeypatch.setattr(bootstrap.socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 0))])
    with pytest.raises(OSError):
        bootstrap.BootstrapListener('localhost', 0)


def test_other_bind_error_closes_already_reserved_sockets(monkeypatch):
    original = bootstrap._BootstrapServer.server_bind
    sockets = []

    def bind(server):
        sockets.append(server.socket)
        if len(sockets) == 2:
            raise OSError(errno.EACCES, 'controlled bind denial')
        original(server)

    monkeypatch.setattr(bootstrap.socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.2', 0))])
    monkeypatch.setattr(bootstrap._BootstrapServer, 'server_bind', bind)
    with pytest.raises(OSError, match='controlled bind denial'):
        bootstrap.BootstrapListener('localhost', 0)
    assert len(sockets) == 2
    assert all(sock.fileno() == -1 for sock in sockets)
