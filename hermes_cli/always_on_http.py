"""Bounded, direct loopback HTTP reads for the stdlib-only backend roller.

Socket timeouts alone bound inactivity, not a peer making slow progress. Rearm
from one monotonic deadline at every raw read, including HTTP header parsing.
No redirects, environment proxies, DNS, reader threads or detached futures.
JSON decoding is size-bounded, not preemptible; expiry after decode rejects
the result instead of treating late evidence as successful readiness.
"""
from __future__ import annotations

import http.client
import io
import json
import socket
import time

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock: socket.socket, deadline: float):
        self.sock, self.deadline = sock, deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("loopback transfer deadline")
        self.sock.settimeout(left)
        return self.sock.recv_into(buffer)


class _ResponseSocket:
    def __init__(self, sock: socket.socket, deadline: float):
        self.sock, self.deadline = sock, deadline

    def makefile(self, mode: str):
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))


def get_json(port: int, path: str, token: str, timeout: float) -> tuple[int | None, float, dict | None]:
    started = time.monotonic()
    deadline = started + timeout
    try:
        if timeout <= 0 or not path.startswith("/") or any(c in path + token for c in "\r\n"):
            raise ValueError("invalid loopback request")
        request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{int(port)}\r\n"
                   f"X-Hermes-Session-Token: {token}\r\nConnection: close\r\n\r\n").encode("ascii")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(max(0.000001, deadline - time.monotonic()))
            sock.connect(("127.0.0.1", int(port)))
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("loopback connect deadline")
            sock.settimeout(left)
            sock.sendall(request)
            with http.client.HTTPResponse(_ResponseSocket(sock, deadline)) as response:
                response.begin()
                status = response.status
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES or (response.length is not None and response.length != 0):
                    raise ValueError("oversized or truncated loopback response")
        if time.monotonic() >= deadline:
            raise TimeoutError("loopback response deadline")
        parsed = None
        if body[:1] in (b"{", b"["):
            parsed = json.loads(body)
        if time.monotonic() >= deadline:
            raise TimeoutError("loopback decode deadline")
        return status, time.monotonic() - started, parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return None, time.monotonic() - started, None
