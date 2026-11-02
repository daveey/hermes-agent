"""External resolution is opt-in; local HTTP and explicit resolver fakes remain usable."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

import http.server
import socket
import threading
import urllib.request

import pytest


@pytest.mark.parametrize("host", ["openrouter.ai", "api.openai.com", "api.anthropic.com"])
def test_external_name_lookup_requires_a_transport_stub(host):
    with pytest.raises(socket.gaierror, match="test fence") as error:
        socket.getaddrinfo(host, 443)
    message = str(error.value)
    assert host in message
    assert "stub the HTTP layer" in message
    assert "mark the test live" in message


@pytest.mark.live  # Tests the opt-out itself using a resolver spy, never a live service.
def test_live_marker_leaves_the_installed_resolver_untouched(request, monkeypatch):
    from tests.conftest import _no_live_nous_services

    calls = []
    expected = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    def resolver(host, port):
        calls.append((host, port))
        return expected

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    _no_live_nous_services.__wrapped__(request, monkeypatch)
    assert socket.getaddrinfo("openrouter.ai", 443) == expected
    assert calls == [("openrouter.ai", 443)]


def test_loopback_http_server_is_reachable():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"loopback works")

        def log_message(self, format, *args):
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://localhost:{server.server_port}/", timeout=5) as response:
                assert response.status == 200
                assert response.read() == b"loopback works"
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()


def test_explicit_test_resolver_can_supply_a_fake_hostname(monkeypatch):
    expected = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    def resolver(host, port):
        assert (host, port) == ("service.example.test", 443)
        return expected

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    assert socket.getaddrinfo("service.example.test", 443) == expected
