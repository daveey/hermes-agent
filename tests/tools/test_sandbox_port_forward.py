"""The sandbox port forward (tools/environments/streams.py) reports connections that never reached their
target: the sandbox browser's liveness is re-checked only after one (tools/browser_session_sandbox.py)."""

import socket
import subprocess
import sys
import threading

import pytest

from tests.waiting import wait_until
from tools.environments import streams


@pytest.fixture
def local_relay(monkeypatch):
    """The relay runs on this host instead of inside a sandbox: same script, same pipes."""
    def open_stream(env, argv, **kw):
        return subprocess.Popen([sys.executable, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    monkeypatch.setattr(streams, "open_stream", open_stream)
    opened = []
    yield opened
    for env, port in opened:
        streams._forwards.pop((id(env), "127.0.0.1", port)).close()


@pytest.fixture
def closed_port():
    # Bound but not listening: connections fail without releasing the port for
    # another parallel test's one-shot server to inherit before our probe.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        yield s.getsockname()[1]


def test_closed_port_is_reserved_until_the_probe_finishes(closed_port):
    with socket.socket() as contender:
        with pytest.raises(OSError):
            contender.bind(("127.0.0.1", closed_port))


def _one_shot_server(reply: bytes, *, read_request: bool = False) -> int:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        with srv:
            while True:
                conn, _ = srv.accept()
                with conn:
                    # A parallel test may probe a previously free port. An empty
                    # connection is not our HTTP request and must not consume it.
                    if read_request and not conn.recv(65536):
                        continue
                    conn.sendall(reply)
                    return
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def _relay(local_port: int, request: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", local_port), timeout=10) as conn:
        conn.settimeout(10)
        conn.sendall(request)
        conn.shutdown(socket.SHUT_WR)  # the client is done sending; the relay ends with the target's reply
        chunks = []
        while chunk := conn.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


def test_only_a_connection_that_never_reaches_its_target_counts_as_a_forward_failure(local_relay, closed_port):
    env = object()
    live = _one_shot_server(b"devtools")
    local_relay.append((env, live))
    assert _relay(streams.forward_port(env, live)) == b"devtools"

    dead = closed_port  # Chromium died: nothing listens on its DevTools port any more
    local_relay.append((env, dead))
    assert _relay(streams.forward_port(env, dead)) == b""

    wait_until(lambda: streams.forward_failures(env, dead), what="the dead port's forward failure")
    assert streams.forward_failures(env, dead) == 1
    assert streams.forward_failures(env, live) == 0


_NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"


def test_an_empty_port_probe_cannot_consume_the_http_reply(local_relay):
    """Another test's stale closed-port probe can land on our newly bound port."""
    env = object()
    target = _one_shot_server(_NOT_FOUND, read_request=True)
    local_relay.append((env, target))
    with socket.create_connection(("127.0.0.1", target), timeout=10) as probe:
        probe.shutdown(socket.SHUT_WR)
        # EOF orders the probe before the real request, without scheduling sleeps.
        while probe.recv(65536):
            pass
    assert _relay(streams.forward_port(env, target), b"GET /missing HTTP/1.1\r\n\r\n") == _NOT_FOUND
    assert streams.forward_failures(env, target) == 0


def test_a_404_on_the_old_browser_path_counts_as_a_forward_failure(local_relay):
    """Chromium relaunched on the same port answers the cached ``/devtools/browser/<id>`` with 404: the
    endpoint the session holds is gone although the port relayed bytes."""
    env = object()
    stale = _one_shot_server(_NOT_FOUND, read_request=True)
    local_relay.append((env, stale))
    request = b"GET /devtools/browser/old-id HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n\r\n"
    assert _relay(streams.forward_port(env, stale), request).startswith(b"HTTP/1.1 404")
    wait_until(lambda: streams.forward_failures(env, stale) == 1, what="the stale-path 404 counted as a failure")

    other = _one_shot_server(_NOT_FOUND, read_request=True)  # a dev server's own 404 is a relayed reply
    local_relay.append((env, other))
    assert _relay(streams.forward_port(env, other), b"GET /missing HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 404")
    live = _one_shot_server(b"HTTP/1.1 101 Switching Protocols\r\n\r\n", read_request=True)
    local_relay.append((env, live))
    assert _relay(streams.forward_port(env, live), request).startswith(b"HTTP/1.1 101")
    assert streams.forward_failures(env, other) == 0 and streams.forward_failures(env, live) == 0
