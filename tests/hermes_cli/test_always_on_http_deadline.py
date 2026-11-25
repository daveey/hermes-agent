"""Real loopback transfer bounds for startup probes, including slow progress."""
from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from hermes_cli import always_on_rollover as ro


@contextmanager
def peer(reply, *, trickle=False):
    stop = threading.Event()
    closed = threading.Event()
    received = []
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        port = listener.getsockname()[1]

        def serve():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(2)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += conn.recv(4096)
                    received.append(request)
                    for part in reply:
                        conn.sendall(part)
                        if trickle and stop.wait(.04):
                            break
                    # Detect client closure, not just a returned timeout tuple.
                    if conn.recv(1) == b"":
                        closed.set()
            except (BrokenPipeError, ConnectionResetError):
                closed.set()
            except OSError as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            yield port, received, closed
        finally:
            stop.set()
            thread.join(3)
            assert not thread.is_alive()
            assert not errors


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_get_total_deadline_closes_trickling_peer(phase, tmp_path):
    if phase == "headers":
        parts = [b"HTTP/1.1 200 OK\r\nX-Slow: "] + [b"a"] * 15 + [
            b"\r\nContent-Length: 2\r\n\r\n{}"]
    else:
        parts = [b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n"] + [b" "] * 15
    roller = ro.Roller(token="fixture-token", checkout=None, marker=tmp_path / "marker")
    with peer(parts, trickle=True) as (port, received, closed):
        start = time.monotonic()
        status, elapsed, body = roller._http_get(port, "/api/health", .12)
        duration = time.monotonic() - start
        assert duration < .30, (phase, duration)
        assert status is None and body is None
        assert elapsed < .30
        assert closed.wait(1)
        assert b"X-Hermes-Session-Token: fixture-token\r\n" in received[0]


def test_decode_crossing_deadline_cannot_return_success(monkeypatch, tmp_path):
    from hermes_cli import always_on_http as transport
    original_clock = time.monotonic
    original_loads = json.loads
    offset = [0.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: original_clock() + offset[0])

    def decode(body):
        result = original_loads(body)
        offset[0] += .2  # decoding itself consumed the remaining budget
        return result

    monkeypatch.setattr(transport.json, "loads", decode)
    roller = ro.Roller(token="fixture", checkout=None, marker=tmp_path / "marker")
    with peer([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"]) as (port, _, closed):
        status, _, body = roller._http_get(port, "/api/health", .12)
        assert status is None and body is None
        assert closed.wait(1)


def test_malformed_json_is_unavailable(tmp_path):
    body = b'{"nested":[}'
    reply = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    roller = ro.Roller(token="fixture", checkout=None, marker=tmp_path / "marker")
    with peer([reply]) as (port, _, closed):
        status, _, parsed = roller._http_get(port, "/api/health", 1)
        assert status is None and parsed is None
        assert closed.wait(1)


@pytest.mark.parametrize("status", [200, 401, 503])
def test_get_preserves_status_and_json(status, tmp_path):
    body = json.dumps({"ok": status == 200}).encode()
    reply = (f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    roller = ro.Roller(token="fixture-token", checkout=None, marker=tmp_path / "marker")
    with peer([reply]) as (port, _received, closed):
        actual, _elapsed, parsed = roller._http_get(port, "/api/health", 1)
        assert actual == status
        if status == 200:
            assert parsed == {"ok": True}
        assert closed.wait(1)
