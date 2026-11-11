"""Actual HTTP binary protocol, including credential redirect refusal."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from hermes_cli import kanban_sync_attachment_http as transport
from hermes_cli.kanban_sync_client import Hub, HubError, Identity


@pytest.fixture
def server():
    state = {"calls": [], "body": b"\x00\xffproof", "redirect": False}

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"].append((self.command, self.path, self.headers["Authorization"], data))
            digest = hashlib.sha256(data).hexdigest()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"sha256": digest, "blob_key": "sha256:" + digest, "size": len(data)}).encode())

        def do_GET(self):
            state["calls"].append((self.command, self.path, self.headers["Authorization"]))
            if state.get("truncated"):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"20\r\npartial")
                self.close_connection = True
                return
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    hub = Hub(Identity(f"http://127.0.0.1:{httpd.server_port}", "fixture-key", "worker"))
    try:
        yield hub, state
    finally:
        hub.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_binary_roundtrip_uses_enrolled_key_and_exact_path(server):
    hub, state = server
    data = state["body"]
    digest = hashlib.sha256(data).hexdigest()
    assert hub.put_attachment("board", "uid", digest, data) == {
        "blob_key": "sha256:" + digest, "sha256": digest, "size": len(data)}
    assert hub.get_attachment_blob("board", "uid") == data
    assert state["calls"] == [
        ("PUT", f"/v2/boards/board/attachments/uid/blobs/{digest}", "Bearer fixture-key", data),
        ("GET", "/v2/boards/board/attachments/uid/blob", "Bearer fixture-key")]


def test_redirect_never_replays_credentials(server):
    hub, state = server
    state["redirect"] = True
    with pytest.raises(HubError):
        hub.get_attachment_blob("board", "uid")
    assert len(state["calls"]) == 1


def test_truncated_chunked_response_is_retryable_io_failure(server):
    hub, state = server
    state["truncated"] = True
    with pytest.raises(OSError):
        hub.get_attachment_blob("board", "uid")


def test_body_size_is_capped_without_content_length(server, monkeypatch):
    hub, state = server
    monkeypatch.setattr(transport, "KANBAN_ATTACHMENT_MAX_BYTES", 2)
    with pytest.raises(ValueError, match="size limit"):
        hub.get_attachment_blob("board", "uid")
    with pytest.raises(ValueError, match="size limit"):
        hub.put_attachment("board", "uid", "0" * 64, state["body"])
    assert len(state["calls"]) == 1
