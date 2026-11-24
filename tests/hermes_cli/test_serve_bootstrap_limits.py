"""Bootstrap clients cannot hold readiness or steal a Windows listener."""
import http.client
import socket
import threading
import time
from unittest.mock import Mock

import pytest

from hermes_cli import serve_bootstrap_listener as bootstrap


def test_dribbling_client_cannot_hold_health_or_handoff(monkeypatch):
    accepted = threading.Event()
    original = bootstrap._Handler.setup

    def setup(handler):
        original(handler)
        accepted.set()

    monkeypatch.setattr(bootstrap._Handler, 'setup', setup)
    with bootstrap.BootstrapListener('127.0.0.1', 0) as listener:
        stop = threading.Event()
        with socket.create_connection(('127.0.0.1', listener.port), timeout=2) as client:
            def dribble(target=client):
                try:
                    while not stop.wait(0.02):
                        target.sendall(b'G')
                except OSError:
                    pass

            sender = threading.Thread(target=dribble, daemon=True)
            sender.start()
            try:
                assert accepted.wait(2)
                from tests.hermes_cli.test_serve_bootstrap_listener import request
                started = time.monotonic()
                assert request(listener.port, '/api/health')[0] == 503
                assert time.monotonic() - started < 1.5
                # A second endless request must not block the socket transfer.
                accepted.clear()
                with socket.create_connection(('127.0.0.1', listener.port), timeout=2) as second:
                    second.sendall(b'G')
                    assert accepted.wait(2)
                    second_sender = threading.Thread(target=dribble, args=(second,), daemon=True)
                    second_sender.start()
                    try:
                        started = time.monotonic()
                        assert listener.handoff().fileno() >= 0
                        assert time.monotonic() - started < 1.5
                    finally:
                        stop.set()
                        second_sender.join(2)
                        assert not second_sender.is_alive()
            finally:
                stop.set()
                sender.join(2)
                assert not sender.is_alive()


@pytest.mark.platforms('windows')
def test_windows_exclusive_bind_never_enables_reuseaddr():
    seen = []

    class FakeSocket:
        def setsockopt(self, level, option, value):
            seen.append((option, value))

        def bind(self, address):
            seen.append(('bind', address))

        def getsockname(self):
            return ('127.0.0.1', 1234)

    server = object.__new__(bootstrap._BootstrapServer)
    server.socket = FakeSocket()
    server.server_address = ('127.0.0.1', 1234)
    server.server_bind()
    assert seen == [(socket.SO_EXCLUSIVEADDRUSE, 1), ('bind', ('127.0.0.1', 1234))]


@pytest.mark.parametrize('error', [OSError(10048, 'address in use'), OSError(98, 'address in use')])
def test_platform_bind_conflict_keeps_machine_sentinel(monkeypatch, error):
    from hermes_cli import web_server_lifecycle as lifecycle
    report = Mock()
    monkeypatch.setattr(lifecycle, '_report_port_in_use', report)
    monkeypatch.setattr(bootstrap._BootstrapServer, 'server_bind', Mock(side_effect=error))
    with pytest.raises(SystemExit) as exited:
        bootstrap.BootstrapListener('127.0.0.1', 9876)
    assert exited.value.code == lifecycle.PORT_IN_USE_EXIT_CODE
    report.assert_called_once_with('127.0.0.1', 9876)


def test_localhost_retains_every_resolved_loopback():
    addresses = socket.getaddrinfo('localhost', 0, type=socket.SOCK_STREAM)
    with bootstrap.BootstrapListener('localhost', 0) as listener:
        assert {server.address_family for server in listener.servers} == {row[0] for row in addresses}
        assert listener.server.request_queue_size >= 2048
        for _, _, _, _, address in addresses:
            client = http.client.HTTPConnection(address[0], listener.port, timeout=2)
            try:
                client.request('GET', '/api/health')
                response = client.getresponse()
                assert response.status == 503
                response.read()
            finally:
                client.close()
        sockets = [server.socket for server in listener.servers]
        listener.handoff()
        assert all(sock.fileno() >= 0 for sock in sockets)
    assert all(sock.fileno() == -1 for sock in sockets)


def test_port_conflict_hint_stays_on_stderr(monkeypatch, capsys):
    from hermes_cli import web_server_lifecycle as lifecycle
    sentinels = []
    monkeypatch.setattr(lifecycle, '_write_machine_sentinel_line', sentinels.append)
    lifecycle._report_port_in_use('127.0.0.1', 9876)
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Port 9876 on 127.0.0.1 is already in use' in output.err
    assert len(sentinels) == 1


@pytest.mark.platforms('linux', 'macos', 'windows')
def test_second_listener_cannot_share_live_port():
    from hermes_cli.web_server_lifecycle import PORT_IN_USE_EXIT_CODE
    with bootstrap.BootstrapListener('127.0.0.1', 0) as first:
        with pytest.raises(SystemExit) as exited:
            bootstrap.BootstrapListener('127.0.0.1', first.port)
        assert exited.value.code == PORT_IN_USE_EXIT_CODE
