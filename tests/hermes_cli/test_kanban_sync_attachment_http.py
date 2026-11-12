"""Actual HTTP binary protocol, including credential redirect refusal."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

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
            if state.get("trickle"):
                try:
                    if state["trickle"] == "headers":
                        payload = b"HTTP/1.0 200 OK\r\nX-Slow: " + b"a" * 30 + b"\r\n\r\n"
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", "30")
                        self.end_headers()
                        payload = b"a" * 30
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if state["stop"].wait(0.05):
                            break
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    state["finished"].set()
                return
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
        if "stop" in state:
            state["stop"].set()
            assert state["finished"].wait(2)
        hub.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_absolute_deadline_cancels_trickling_transfer(server, phase):
    hub, state = server
    state.update(trickle=phase, stop=threading.Event(), finished=threading.Event())
    hub.deadline = time.monotonic() + 0.3
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        hub.get_attachment_blob("board", "uid")
    assert time.monotonic() - started < 1.0
    # The server sees connection shutdown, not just an abandoned daemon reader.
    assert state["finished"].wait(0.8)
    assert len(state["calls"]) == 1


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


def test_connection_finishing_after_expiry_cannot_send():
    from hermes_cli.kanban_sync_attachment_deadline import Transfer
    import socket

    transfer = Transfer(0.02)
    release = threading.Event()
    connected = threading.Event()
    closed = threading.Event()
    left, right = socket.socketpair()

    class SlowConnection:
        sock = left

        def connect(self):
            connected.set()
            assert release.wait(2)

        def close(self):
            left.close()
            closed.set()

    connection = transfer.connection(SlowConnection)()
    try:
        with pytest.raises(TimeoutError):
            transfer.run(connection.connect)
        assert connected.is_set()
        release.set()
        assert closed.wait(1)
        right.settimeout(1)
        assert right.recv(1) == b""
    finally:
        release.set()
        left.close()
        right.close()


def test_body_size_is_capped_without_content_length(server, monkeypatch):
    hub, state = server
    monkeypatch.setattr(transport, "KANBAN_ATTACHMENT_MAX_BYTES", 2)
    with pytest.raises(ValueError, match="size limit"):
        hub.get_attachment_blob("board", "uid")
    with pytest.raises(ValueError, match="size limit"):
        hub.put_attachment("board", "uid", "0" * 64, state["body"])
    assert len(state["calls"]) == 1
