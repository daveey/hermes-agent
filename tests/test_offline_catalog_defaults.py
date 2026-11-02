"""Incidental catalog defaults keep real fallbacks and explicit HTTP fakes usable."""
import http.server
import threading
from types import SimpleNamespace

import pytest
import requests

from plugins.video_gen import openrouter


def test_video_catalog_default_exercises_bundled_fallback_without_dns(monkeypatch):
    import socket

    def unexpected_dns(*args, **kwargs):
        pytest.fail("the offline catalog must not resolve a remote endpoint")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    provider = openrouter.OpenRouterVideoGenProvider()
    monkeypatch.setattr(provider, "_credentials", lambda: ("fake", "https://openrouter.ai/api/v1"))
    assert provider._catalog() is openrouter._FALLBACK_CATALOG
    assert provider._catalog_cache is None


def test_explicit_video_catalog_transport_preserves_cache_and_expiry(monkeypatch):
    clock = [100.0]
    calls = []
    entries = [{"id": "example/video", "supported_durations": [4]}]

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": entries})

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(openrouter, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    provider = openrouter.OpenRouterVideoGenProvider()
    monkeypatch.setattr(provider, "_credentials", lambda: ("fake", "https://catalog.example.test/v1"))
    assert provider._catalog() == entries
    assert provider._catalog() == entries
    assert calls == ["https://catalog.example.test/v1/videos/models"]
    clock[0] += openrouter._CATALOG_TTL_S
    assert provider._catalog() == entries
    assert calls == ["https://catalog.example.test/v1/videos/models"] * 2


def test_offline_catalog_default_preserves_real_loopback_http():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data": [{"id": "local/video"}]}')

        def log_message(self, format, *args):
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            response = requests.get(f"http://127.0.0.1:{server.server_port}/videos/models", timeout=5)
            assert response.json() == {"data": [{"id": "local/video"}]}
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
