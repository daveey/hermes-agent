"""Opt-in resolver for tests whose code under test resolves a URL's host itself.

The suite's network fence (``_fenced_getaddrinfo`` in tests/conftest.py) refuses every
lookup of a host off this machine. SSRF guards (``tools.url_safety``, the
browser URL policy, Slack's file-URL check) judge a URL by the address its host resolves
to, so under the fence they see a refusal and fail closed. A test of the *allowed* path
requests ``public_dns``, which answers without touching the network:

- IP literals resolve numerically (``AI_NUMERICHOST``: parsed, never sent to DNS);
- ``localhost`` resolves through the real resolver (loopback, /etc/hosts);
- any other name resolves to ``PUBLIC_TEST_ADDRESS`` unless the test maps it, e.g.
  ``public_dns["internal.example"] = "10.0.0.5"`` to model a rebinding answer.

The transport stays the test's job: a resolved address is never dialled by this fixture.
"""
from __future__ import annotations

import socket

import pytest

# A globally routable address (example.com's long-standing A record), so SSRF policy
# classifies it as public. Tests never connect to it: they stub the HTTP layer.
PUBLIC_TEST_ADDRESS = "93.184.216.34"

# Captured at import, before any test's fence replaces socket.getaddrinfo.
_REAL_GETADDRINFO = socket.getaddrinfo


def _numeric(host, port, family, type, proto, flags):
    return _REAL_GETADDRINFO(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)


@pytest.fixture
def public_dns(monkeypatch) -> dict[str, str]:
    """Resolve names offline; returns the name -> address table the test may extend."""
    answers: dict[str, str] = {}

    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        name = (host.decode() if isinstance(host, bytes) else str(host or "")).lower().rstrip(".")
        if name == "localhost":
            return _REAL_GETADDRINFO(host, port, family, type, proto, flags)
        try:
            return _numeric(host, port, family, type, proto, flags)
        except socket.gaierror:
            pass
        return _numeric(answers.get(name, PUBLIC_TEST_ADDRESS), port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return answers
