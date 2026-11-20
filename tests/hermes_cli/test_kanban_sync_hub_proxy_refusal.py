"""Real proxy wire failures preserve Hub's typed transport-refusal contract."""
from __future__ import annotations

import http.client
import http.server
import threading
import time
import urllib.error
import urllib.request

import pytest

from hermes_cli import kanban_sync_client as ksc


@pytest.fixture
def proxy(monkeypatch):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            self.rfile.read(int(self.headers.get('content-length', 0)))
            self.server.requests.append((self.command, self.path))
            self.wfile.write(self.server.response)
            self.close_connection = True

        do_GET = do_POST = respond

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests = []
    server.response = b''
    for name in ('http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY',
                 'all_proxy', 'ALL_PROXY', 'no_proxy', 'NO_PROXY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('http_proxy', f'http://127.0.0.1:{server.server_port}')
    # urlopen caches its opener; don't inherit another test's proxy selection.
    monkeypatch.setattr(urllib.request, '_opener', None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def hub_for_proxy(monkeypatch, deadline):
    def forbid_direct(*args, **kwargs):
        pytest.fail('Proxy request incorrectly used kept-alive direct transport')

    monkeypatch.setattr(ksc.Hub, '_call_kept_alive', forbid_direct)
    return ksc.Hub(ksc.Identity(url='http://board.invalid', key='synthetic-key', gateway='fixture'),
                   timeout=3, deadline=time.monotonic() + 3 if deadline else None)


@pytest.mark.parametrize('deadline', [False, True], ids=['urlopen', 'deadline-opener'])
@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize(('wire', 'cause'), [
    (b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}', http.client.IncompleteRead),
    (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{}', http.client.IncompleteRead),
    (b'garbled status\r\n\r\n', http.client.BadStatusLine),
    # HTTPError.read() is another transport boundary, not a completed HTTP refusal.
    (b'HTTP/1.1 409 Conflict\r\nContent-Length: 20\r\n\r\n{}', http.client.IncompleteRead),
])
def test_proxy_protocol_failure_is_urlerror(proxy, monkeypatch, deadline, method, wire, cause):
    proxy.response = wire
    hub = hub_for_proxy(monkeypatch, deadline)
    try:
        with pytest.raises(urllib.error.URLError) as caught:
            hub._call(method, '/synthetic', {'fixture': True} if method == 'POST' else None)
        assert isinstance(caught.value.reason, cause)
        assert caught.value.__cause__ is caught.value.reason
        assert proxy.requests == [(method, 'http://board.invalid/synthetic')]
    finally:
        hub.close()


@pytest.mark.parametrize('status', [200, 409, 410])
def test_proxy_complete_http_responses_keep_their_contract(proxy, monkeypatch, status):
    proxy.response = f'HTTP/1.1 {status} Fixture\r\nContent-Length: 2\r\n\r\n'.encode() + b'{}'
    hub = hub_for_proxy(monkeypatch, True)
    try:
        if status == 200:
            assert hub._call('GET', '/synthetic') == {}
        else:
            error = ksc.Resync if status == 410 else ksc.HubError
            with pytest.raises(error):
                hub._call('GET', '/synthetic')
        assert proxy.requests == [('GET', 'http://board.invalid/synthetic')]
    finally:
        hub.close()
