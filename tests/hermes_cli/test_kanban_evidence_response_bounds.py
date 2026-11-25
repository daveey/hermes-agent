"""Opt-in evidence transport limits, exercised through both real HTTP paths."""
import gzip
import io
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_sync_hub_transport import BoundedResponseError, read_response


@pytest.fixture
def endpoint():
    state = SimpleNamespace(body=b'{}', redirect_body=b"", encoding=None, status=200, framing="length", requests=[])

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            state.requests.append((self.command, self.path))
            status = 200 if self.path == "/final" else state.status
            body = state.redirect_body if status == 302 else state.body
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/final")
            if state.encoding:
                self.send_header("Content-Encoding", state.encoding)
            if state.framing == "length":
                self.send_header("Content-Length", str(len(body)))
            elif state.framing == "chunked":
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            try:
                if state.framing == "chunked":
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the cap deliberately closes an incompletely drained reply

        do_POST = do_GET

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.fixture(params=[False, True], ids=["keepalive", "urlopen"])
def hub(request, endpoint, monkeypatch):
    monkeypatch.setattr(client.Hub, "_proxied", staticmethod(lambda parts: request.param))
    instance = client.Hub(client.Identity(endpoint.url, "test-key", "test-gateway"), timeout=2)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("framing", ["length", "chunked", "close"])
def test_wire_cap_precedes_json_and_never_replays(hub, endpoint, monkeypatch, framing):
    endpoint.framing = framing
    endpoint.body = b'x' * 10000
    def forbidden(*args, **kwargs):
        pytest.fail("oversize response reached JSON parsing")
    with monkeypatch.context() as patch:
        patch.setattr(client.json, "loads", forbidden)
        with pytest.raises(BoundedResponseError, match="wire byte"):
            hub.row_evidence("board", "task_events", "event", max_response_bytes=256)
    assert len(endpoint.requests) == 1
    assert getattr(hub._local, "conn", None) is None
    endpoint.body = b'{"ok":true}'
    assert hub.row_evidence("board", "task_events", "next", max_response_bytes=256) == {"ok": True}
    assert len(endpoint.requests) == 2


@pytest.mark.parametrize("encoding", [None, "gzip", "deflate"])
def test_valid_boundary_including_compressed_response(hub, endpoint, encoding):
    raw = json.dumps({"payload": "x" * 4000}).encode()
    endpoint.encoding = encoding
    endpoint.body = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw) if encoding else raw
    assert hub.row_evidence("board", "task_events", "event", max_response_bytes=len(raw)) == json.loads(raw)
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_expansion_cap_precedes_json(hub, endpoint, monkeypatch, encoding):
    raw = b'{"payload":"' + b'x' * 100000 + b'"}'
    endpoint.encoding = encoding
    endpoint.body = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)
    assert len(endpoint.body) < 1024
    with monkeypatch.context() as patch:
        patch.setattr(client.json, "loads", lambda *a, **kw: pytest.fail("decoded oversize parsed"))
        with pytest.raises(BoundedResponseError, match="decoded byte"):
            hub.row_evidence("board", "task_events", "event", max_response_bytes=1024)
    assert len(endpoint.requests) == 1
    assert getattr(hub._local, "conn", None) is None


@pytest.mark.parametrize("status", [410, 500])
def test_error_body_is_bounded_too(hub, endpoint, status):
    endpoint.status = status
    endpoint.body = b'x' * 10000
    with pytest.raises(BoundedResponseError):
        hub.row_evidence("board", "task_events", "event", max_response_bytes=256)
    assert len(endpoint.requests) == 1


def test_existing_uncapped_caller_unchanged(hub, endpoint):
    endpoint.body = json.dumps({"large": "x" * 10000}).encode()
    assert hub.row_evidence("board", "task_events", "event") == json.loads(endpoint.body)


@pytest.mark.parametrize("cap", [0, -1, True, "256", 1.5])
def test_invalid_cap_refuses_before_request(hub, endpoint, cap):
    with pytest.raises(ValueError, match="positive integer"):
        hub.row_evidence("board", "task_events", "event", max_response_bytes=cap)
    assert endpoint.requests == []


def test_capped_post_is_not_resent(hub, endpoint):
    endpoint.body = b'x' * 10000
    with pytest.raises(BoundedResponseError):
        hub._call("POST", "/write", max_response_bytes=256, replay_safe=True)
    assert endpoint.requests == [("POST", "/write")]


def test_get_redirect_retains_cap(hub, endpoint):
    endpoint.status = 302
    endpoint.body = b'x' * 10000
    with pytest.raises(BoundedResponseError):
        hub.row_evidence("board", "task_events", "event", max_response_bytes=256)
    assert endpoint.requests[-1] == ("GET", "/final")
    assert sum(path == "/final" for _, path in endpoint.requests) == 1


def test_redirect_body_itself_is_bounded(hub, endpoint):
    endpoint.status = 302
    endpoint.redirect_body = b'x' * 10000
    with pytest.raises(BoundedResponseError):
        hub.row_evidence("board", "task_events", "event", max_response_bytes=256)
    assert len(endpoint.requests) == 1


def test_capped_post_redirect_never_resends(hub, endpoint):
    endpoint.status = 302
    with pytest.raises(client.HubError):
        hub._call("POST", "/write", max_response_bytes=256)
    assert endpoint.requests == [("POST", "/write")]


@pytest.mark.parametrize("encoding,body", [("gzip", b"bad"), ("deflate", b"bad"), ("br", b"bad")])
def test_invalid_encoding_drops_without_retry(hub, endpoint, encoding, body):
    endpoint.encoding, endpoint.body = encoding, body
    with pytest.raises(BoundedResponseError):
        hub.row_evidence("board", "task_events", "event", max_response_bytes=256)
    assert len(endpoint.requests) == 1
    assert getattr(hub._local, "conn", None) is None


def test_reader_uses_explicit_cap_even_without_length_header():
    class Response:
        headers = {}
        def read(self, size):
            assert size == 257
            return b'x' * size
    with pytest.raises(BoundedResponseError):
        read_response(Response(), 256)


def test_multimember_gzip_and_truncated_deflate():
    data = b'{"ok":true}'
    wire = gzip.compress(data[:5]) + gzip.compress(data[5:])
    response = SimpleNamespace(headers={"Content-Encoding": "gzip"}, read=io.BytesIO(wire).read)
    assert read_response(response, 256) == data
    wire = zlib.compress(data)[:-1]
    response = SimpleNamespace(headers={"Content-Encoding": "deflate"}, read=io.BytesIO(wire).read)
    with pytest.raises(BoundedResponseError):
        read_response(response, 256)
