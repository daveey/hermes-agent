"""``hermes plugins enable`` must say when the Desktop backend did not load the plugin.

``notify_serve_backend`` swallowed every failure at debug level, so a backend that refused the
activation (a gated dashboard answering 401 ``no_cookie``) left the CLI printing only the
gateway's success while open Desktop chats never got the plugin.
"""
from __future__ import annotations

import http.server
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import plugins_activation


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 401
    seen_tokens: list = []

    def do_POST(self):
        type(self).seen_tokens.append(self.headers.get("X-Hermes-Session-Token"))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.status == 200:
            body = json.dumps({"ok": True, "activation": {
                "name": "probe", "key": "probe", "activated_now": {"hooks": ["pre_tool_call"]},
                "deferred": {}}}).encode()
        else:
            body = json.dumps({"error": "unauthenticated", "reason": "no_cookie"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def backend(monkeypatch):
    """A fake dashboard backend behind a real host record shape."""
    def start(status):
        handler = type("H", (_Handler,), {"status": status, "seen_tokens": []})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        record = SimpleNamespace(role="desktop-serve", host="127.0.0.1", port=server.server_address[1])
        monkeypatch.setattr(plugins_activation, "_serve_backend_record", lambda: record)
        monkeypatch.setattr("gateway.host_rendezvous.read_token", lambda role: "s3cret-token-value")
        started.append(server)
        return handler
    started: list = []
    yield start
    for server in started:
        server.shutdown()


@pytest.fixture(autouse=True)
def gateway_reloads(monkeypatch):
    monkeypatch.setattr("gateway.control_socket.reload_gateway_plugins",
                        lambda target, *, profile_home=None, timeout=30.0: {
                            "reloaded": True, "plugins": ["probe"], "activations": [
                                {"name": "probe", "key": "probe", "activated_now": {"hooks": 1},
                                 "deferred": {}}]})


def test_backend_refusal_is_logged_and_named_in_the_hint(backend, caplog):
    handler = backend(401)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins_activation"):
        result = plugins_activation.activate_plugin_now("probe", in_process=False)
    hint = plugins_activation.activation_hint(result)

    assert handler.seen_tokens == ["s3cret-token-value"]
    assert ("The Desktop backend did not load it (401 Unauthorized); "
            "open Desktop chats get it after that backend restarts.") in hint
    assert "Gateway reloaded plugins" in hint  # the gateway's half is still reported
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("401 Unauthorized" in m for m in warnings), warnings
    assert not any("s3cret-token-value" in m for m in warnings)  # never log the token
    assert "s3cret-token-value" not in hint


def test_backend_activation_is_shown(backend):
    backend(200)
    hint = plugins_activation.activation_hint(plugins_activation.activate_plugin_now("probe", in_process=False))
    assert "Desktop backend loaded it — active in open Desktop chats now: hooks." in hint
    assert "did not load it" not in hint


def test_no_backend_record_says_nothing_new(monkeypatch, caplog):
    monkeypatch.setattr(plugins_activation, "_serve_backend_record", lambda: None)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins_activation"):
        result = plugins_activation.activate_plugin_now("probe", in_process=False)
    hint = plugins_activation.activation_hint(result)
    assert "Desktop backend" not in hint
    assert hint == "Gateway reloaded plugins — active in the running gateway now: hooks."
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
