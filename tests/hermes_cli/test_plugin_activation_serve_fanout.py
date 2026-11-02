"""A plugin activation reaches EVERY chat-hosting backend on the host (t_01a19a63867979de).

David-MBP ran a host-owner ``hermes serve`` (record ``serve``, :9119) and the Desktop child
(record ``desktop-serve``) at once. ``notify_serve_backend`` dialed only the first record it found,
so the Desktop child, which hosts the Desktop chats, kept running plugin hooks loaded at its start
while hermes-sync logged ``backend serve:9119 ok``. These tests publish real host records into a
temporary rendezvous dir and dial real HTTP listeners, so the record lookup, token check and
per-backend POST all run.
"""
from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading

import pytest

from gateway import host_rendezvous as hr
from hermes_cli import plugins_activation


def _listener(status: int = 200):
    seen: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            seen.append({"token": self.headers.get("X-Hermes-Session-Token"), **body})
            reply = (json.dumps({"ok": True, "activation": {
                "name": body["name"], "key": body["name"], "activated_now": {"hooks": ["pre_tool_call"]},
                "deferred": {}}}) if status == 200 else json.dumps({"error": "nope"})).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def host(monkeypatch, tmp_path):
    """Publish real ``serve`` / ``desktop-serve`` records (this process, distinct ports + tokens)."""
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    servers = []

    def publish(role: str, status: int = 200, *, port=None):
        server, seen = _listener(status)
        servers.append(server)
        record = hr.publish_record(role, host="127.0.0.1", port=port or server.server_address[1],
                                   token=f"{role}-token")
        assert record is not None
        return seen

    yield publish
    for server in servers:
        server.shutdown()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_both_live_records_get_the_activation(host, tmp_path):
    serve = host(hr.ROLE_SERVE)
    desktop = host(hr.ROLE_DESKTOP_SERVE)

    answer = plugins_activation.notify_serve_backend("probe", tmp_path)

    assert serve == [{"token": "serve-token", "name": "probe", "home": str(tmp_path)}]
    assert desktop == [{"token": "desktop-serve-token", "name": "probe", "home": str(tmp_path)}]
    assert answer["ok"] is True and "backend_error" not in answer
    assert [(b["role"], b["answer"]["ok"]) for b in answer["backends"]] == [
        ("serve", True), ("desktop-serve", True)]


def test_host_owner_only(host, tmp_path):
    serve = host(hr.ROLE_SERVE)
    answer = plugins_activation.notify_serve_backend("probe", tmp_path)
    assert len(serve) == 1
    assert [b["role"] for b in answer["backends"]] == ["serve"]
    assert answer["ok"] is True


def test_desktop_only(host, tmp_path):
    desktop = host(hr.ROLE_DESKTOP_SERVE)
    answer = plugins_activation.notify_serve_backend("probe", tmp_path)
    assert len(desktop) == 1
    assert [b["role"] for b in answer["backends"]] == ["desktop-serve"]


def test_no_record_answers_none(host, tmp_path):
    assert plugins_activation.notify_serve_backend("probe", tmp_path) is None
    assert plugins_activation.notify_serve_backends("probe", tmp_path) == []


def test_dead_host_owner_record_does_not_block_the_desktop_child(host, tmp_path):
    host(hr.ROLE_SERVE)
    desktop = host(hr.ROLE_DESKTOP_SERVE)
    path = hr.record_path(hr.ROLE_SERVE)
    payload = json.loads(path.read_text())
    payload["pid"] = _dead_pid()
    path.write_text(json.dumps(payload))

    answer = plugins_activation.notify_serve_backend("probe", tmp_path)

    assert len(desktop) == 1
    assert [b["role"] for b in answer["backends"]] == ["desktop-serve"]


def test_inconsistent_token_record_is_skipped_and_the_other_still_reached(host, tmp_path):
    serve = host(hr.ROLE_SERVE)
    host(hr.ROLE_DESKTOP_SERVE)
    hr.token_path(hr.ROLE_DESKTOP_SERVE).write_text("torn-restart-token")

    answer = plugins_activation.notify_serve_backend("probe", tmp_path)

    assert len(serve) == 1
    assert [b["role"] for b in answer["backends"]] == ["serve"]


def test_unreachable_backend_is_named_and_the_other_still_activates(host, tmp_path):
    # A live record whose port answers nothing (backend wedged mid-restart): the host owner fails,
    # the Desktop child must still activate, and the folded answer must say which one failed.
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()
    host(hr.ROLE_SERVE, port=closed_port)
    desktop = host(hr.ROLE_DESKTOP_SERVE)

    answer = plugins_activation.notify_serve_backend("probe", tmp_path)

    assert len(desktop) == 1
    assert answer["ok"] is True  # the Desktop child's answer leads
    assert answer["backend_error"].startswith(f"serve:{closed_port} unreachable")
    by_role = {b["role"]: b["answer"] for b in answer["backends"]}
    assert by_role["desktop-serve"]["ok"] is True and "backend_error" in by_role["serve"]


def test_cli_hint_reports_a_failed_desktop_backend_even_when_the_host_owner_loaded_it(
        host, monkeypatch):
    monkeypatch.setattr("gateway.control_socket.reload_gateway_plugins",
                        lambda target, *, profile_home=None, timeout=30.0: None)
    host(hr.ROLE_SERVE)
    host(hr.ROLE_DESKTOP_SERVE, status=401)

    result = plugins_activation.activate_plugin_now("probe", in_process=False)

    assert result["backend"]["error"].startswith("desktop-serve:")
    assert "401 Unauthorized" in result["backend"]["error"]
    assert result["activation"]["name"] == "probe"  # what the host owner loaded is still reported
