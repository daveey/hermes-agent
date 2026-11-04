"""No test reaches a host off this machine unless it is a ``live`` canary (``tests/conftest.py``).

A test home has no Nous identity, so a hosted connector action used to mint a real guest on
the production Portal: seconds per test, red when the Portal was slow, and a new production
guest on every CI run. Every other live host had the same shape: building an agent fetched
OpenRouter / models.dev / model-catalog metadata, and verdicts followed whatever came back.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import http.server
import socket
import threading
import urllib.request

import pytest


def test_first_connector_action_in_a_test_home_mints_no_portal_guest():
    from hermes_cli import anon_auth
    from tools.connectors.gateway.config import ensure_guest_identity

    failure = ensure_guest_identity()

    assert failure and failure.startswith("Hosted connectors could not be set up.")
    assert not anon_auth.has_guest()


@pytest.mark.parametrize("host", [
    "portal.nousresearch.com", "inference-api.nousresearch.com", b"WELCOME-API.NOUSRESEARCH.COM.",
    "telemetry.staging-nousresearch.com", "openrouter.ai", "models.dev", "raw.githubusercontent.com",
    "example.com", b"pypi.org",
])
def test_external_hosts_are_refused_at_the_name_lookup(host):
    with pytest.raises(socket.gaierror, match="test fence"):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "169.254.169.254", None])
def test_loopback_and_address_literals_still_resolve(host):
    assert socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)


@pytest.mark.parametrize("host", ["LOCALHOST.", "api.localhost", "[::1]", socket.gethostname()])
def test_names_of_this_machine_are_not_fenced(host):
    try:
        socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:  # the host's own resolver may not know them; the fence must not refuse
        assert "test fence" not in str(exc)


@pytest.fixture(scope="module")
def _lookup_outside_any_test():
    """Runs before any test-scoped fixture: where a per-test fence had not been installed yet,
    and where a thread a previous test left running does its lookups."""
    try:
        socket.getaddrinfo("openrouter.ai", 443)
    except socket.gaierror as exc:
        return str(exc)
    return "resolved"


def test_lookups_outside_a_test_are_fenced_too(_lookup_outside_any_test):
    assert "test fence" in _lookup_outside_any_test


@pytest.mark.parametrize("host", ["openrouter.ai", "api.openai.com", "api.anthropic.com"])
def test_refusal_names_the_host_and_the_ways_out(host):
    with pytest.raises(socket.gaierror, match="test fence") as error:
        socket.getaddrinfo(host, 443)
    message = str(error.value)
    assert host in message
    assert "fixture" in message
    assert "mark the test live" in message


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


def test_public_dns_serves_names_offline_and_keeps_the_fence_after(public_dns):
    public_dns["internal.example"] = "10.0.0.5"
    assert socket.getaddrinfo("openrouter.ai", 443)[0][4][0] == "93.184.216.34"
    assert socket.getaddrinfo("internal.example", 443)[0][4][0] == "10.0.0.5"
