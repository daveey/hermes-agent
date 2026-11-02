"""Default registry transport stays offline while explicit responses stay real."""
import socket
from types import SimpleNamespace

import pytest
import requests

from agent import models_dev


def test_registry_default_fails_before_dns(monkeypatch):
    calls = []

    def dns(*args, **kwargs):
        calls.append(args)
        raise OSError("unexpected DNS")

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with pytest.raises(requests.ConnectionError, match="offline registry fixture"):
        models_dev._fetch_models_dev_from_network()
    assert calls == []


def test_explicit_registry_response_runs_validation(monkeypatch):
    registry = {"example": {"models": {"text": {"id": "text", "attachment": False}}}}
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: registry, headers={"ETag": "fixture"})

    monkeypatch.setattr(requests, "get", get)
    assert models_dev._fetch_models_dev_from_network() == (registry, "fixture")
    assert calls == [models_dev._get_models_dev_url()]


def test_registry_loopback_transport_still_fetches(monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    registry = {"example": {"models": {"text": {"id": "text"}}}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(registry).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(models_dev, "MODELS_DEV_URL", f"http://127.0.0.1:{server.server_port}/api.json")
    try:
        assert models_dev._fetch_models_dev_from_network() == (registry, "")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

