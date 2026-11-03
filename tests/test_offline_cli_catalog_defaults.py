"""CLI catalog defaults must not consult live manifests or provider inventories."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import socket
import urllib.error
import urllib.request

import pytest

from hermes_cli import model_catalog, models


@pytest.mark.parametrize("kind", ["manifest", "provider"])
def test_cli_catalog_default_does_not_resolve(monkeypatch, kind):
    calls = []

    def dns(*args, **kwargs):
        calls.append(args)
        raise OSError("unexpected DNS")

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    if kind == "manifest":
        assert model_catalog._fetch_manifest("https://catalog.example.test/models.json", 1) is None
    else:
        req = urllib.request.Request("https://provider.example.test/v1/models")
        with pytest.raises(urllib.error.URLError, match="offline CLI catalog fixture"):
            models._urlopen_model_catalog_request(req, timeout=1)
    assert calls == []


@pytest.mark.parametrize("kind", ["manifest", "provider"])
def test_explicit_cli_catalog_transport_runs_parser(monkeypatch, kind):
    import io
    import json

    payload = {"version": 1, "providers": {"example": {"models": [{"id": "text"}]}}}
    calls = []

    def open_response(req, **kwargs):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps(payload).encode())

    url = "https://catalog.example.test/models.json"
    if kind == "manifest":
        monkeypatch.setattr(model_catalog, "_open_manifest_request", open_response)
        assert model_catalog._fetch_manifest(url, 1) == payload
    else:
        monkeypatch.setattr(models, "_urlopen_model_catalog_request", open_response)
        assert models._get_json(url, timeout=1) == payload
    assert calls == [url]


@pytest.mark.parametrize("kind", ["manifest", "provider"])
def test_cli_catalog_loopback_http_still_works(kind):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = {"version": 1, "providers": {"example": {"models": [{"id": "text"}]}}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/models.json"
    try:
        if kind == "manifest":
            assert model_catalog._fetch_manifest(url, 5) == payload
        else:
            assert models._get_json(url, timeout=5) == payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

