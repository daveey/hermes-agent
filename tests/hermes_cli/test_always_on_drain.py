"""Keep-alive drain of a superseded always-on backend (hermes_cli/web_server_skew_exit).

Desktop's keep-alive sockets stay pinned to a superseded backend through the TCP-splice proxy. Retiring it
with them open hung up every in-flight request at once (hub poll 06:47:16Z Nov 13: 9/9 keep-alive
connections RemoteDisconnected), and on the MacBook a hang-up tears down and redials the SSH transport.
While draining, responses carry ``Connection: close`` and retirement waits for the HTTP connections to go.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import threading

import pytest
import uvicorn

from hermes_cli import web_server_skew_exit as skew
from tests.waiting import wait_until


@pytest.fixture(autouse=True)
def _not_draining():
    skew._DRAINING.clear()
    yield
    skew._DRAINING.clear()


# ── the response header ───────────────────────────────────────────────────────────────────────────────


def test_close_headers_adds_or_replaces_connection_but_keeps_an_upgrade():
    assert skew._close_headers([(b"content-type", b"text/plain")]) == [
        (b"content-type", b"text/plain"), (b"connection", b"close")]
    assert skew._close_headers([(b"Connection", b"keep-alive"), (b"x", b"1"), (b"connection", b"x")]) == [
        (b"connection", b"close"), (b"x", b"1")]
    upgrade = [(b"connection", b"Upgrade"), (b"upgrade", b"websocket")]
    assert skew._close_headers(upgrade) == upgrade
    assert skew._close_headers(None) == [(b"connection", b"close")]


def _run(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


async def _inner(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})
    else:
        await send({"type": scope["type"] + ".marker"})


def test_wrapper_adds_close_only_to_http_while_draining():
    state = {"on": False}
    app = skew.wrap_asgi_close_when_draining(_inner, is_draining=lambda: state["on"])
    assert _run(app, {"type": "http"})[0]["headers"] == [(b"content-length", b"2")]
    state["on"] = True
    sent = _run(app, {"type": "http"})
    assert sent[0]["headers"] == [(b"content-length", b"2"), (b"connection", b"close")]
    assert sent[1] == {"type": "http.response.body", "body": b"ok"}
    assert _run(app, {"type": "websocket"}) == [{"type": "websocket.marker"}]
    assert _run(app, {"type": "lifespan"}) == [{"type": "lifespan.marker"}]


# ── the watchdog waits for the drain ──────────────────────────────────────────────────────────────────


class _Server:
    should_exit = False


class _Fence:
    def __init__(self):
        self.prepared = 0

    def prepare(self):
        self.prepared += 1
        return {"ok": True, "token": "t"}

    def commit(self, token):
        return {"ok": True}


class _Clock:
    """Advances one second per poll (each poll reads the clock once while superseded)."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 1.0
        return self.now


def _watch(*, superseded=lambda p: True, connections=lambda: 0, polls=40, **kw):
    server, fence, clock = _Server(), _Fence(), _Clock()
    seen = []

    def check(port):
        seen.append(skew.draining())
        return superseded(port)

    skew.start_superseded_watchdog(server, port=9149, superseded=check, fence=fence, poll_s=0.001,
                                   max_polls=polls, http_connections=connections, clock=clock,
                                   **kw).join(10)
    return server, fence, clock, seen


def test_superseded_backend_drains_before_it_asks_the_fence():
    server, fence, clock, _ = _watch(connections=lambda: 0)
    assert server.should_exit is True and skew.draining() is True
    # first observation at t0; the reason needs DRAIN_MIN_S, then two confirming polls
    assert clock.now - 1001.0 >= skew.DRAIN_MIN_S
    assert fence.prepared == 1


def test_open_connections_hold_retirement_until_they_close():
    calls = {"n": 0}

    def connections():
        calls["n"] += 1
        return 3 if calls["n"] <= 12 else 0

    server, _fence, clock, _ = _watch(connections=connections, polls=40)
    assert server.should_exit is True and calls["n"] > 12
    assert clock.now - 1001.0 >= skew.DRAIN_MIN_S + 12  # waited for the connections, not just the minimum


def test_unknown_or_stuck_connections_retire_at_the_cap():
    for connections in (lambda: None, lambda: 2):
        server, _fence, clock, _ = _watch(connections=connections, polls=60)
        assert server.should_exit is True
        assert skew.DRAIN_CAP_S <= clock.now - 1001.0 <= skew.DRAIN_CAP_S + 3
    server, fence, clock, _ = _watch(connections=lambda: 2, polls=20)
    assert server.should_exit is False and fence.prepared == 0  # under the cap: still draining


def test_an_aborted_roll_stops_the_drain():
    flips = iter([True, True, True] + [False] * 10)
    server, fence, _, seen = _watch(superseded=lambda p: next(flips, False), polls=12)
    assert server.should_exit is False and fence.prepared == 0
    assert seen[1] is True and skew.draining() is False


def test_open_http_connections_counts_http_protocols_only():
    class H11Like:
        pass

    H11Like.__module__ = "uvicorn.protocols.http.h11_impl"

    class WsLike:
        pass

    WsLike.__module__ = "uvicorn.protocols.websockets.websockets_impl"
    server = type("S", (), {})()
    server.server_state = type("St", (), {"connections": {H11Like(), H11Like(), WsLike()}})()
    assert skew.open_http_connections(server) == 2
    assert skew.open_http_connections(object()) is None


# ── a real uvicorn: a pinned keep-alive socket closes after its next response ──────────────────────────


@pytest.fixture
def served():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(skew.wrap_asgi_close_when_draining(_inner), log_level="warning",
                                           log_config=None, lifespan="off"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    wait_until(lambda: server.started, what="uvicorn start")
    yield server, sock.getsockname()[1]
    server.should_exit = True
    thread.join(10)


def test_keep_alive_socket_closes_after_the_next_response_once_draining(served):
    server, port = served
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    first = conn.getresponse()
    assert first.read() == b"ok" and first.getheader("connection") is None
    assert wait_until(lambda: skew.open_http_connections(server) == 1, what="one keep-alive connection")
    skew._DRAINING.set()
    conn.request("GET", "/")  # same pinned socket
    second = conn.getresponse()
    assert second.read() == b"ok" and second.getheader("connection") == "close"
    wait_until(lambda: skew.open_http_connections(server) == 0, what="the server to close the socket")
    conn.close()
