"""CI read budgets cover retries and both artifact hops, not just idle sockets."""
import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from scripts.ci import flaky_artifact as fa
from scripts.ci import flaky_kanban as fk


def test_retry_backoff_cannot_start_another_request_after_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    opening = Mock(side_effect=URLError("unavailable"))
    monkeypatch.setattr(fk.urllib.request, "build_opener", lambda *a: type("Opener", (), {"open": opening})())
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with fa.transport.deadline_scope(100.25), pytest.raises(TimeoutError):
        fk._gh("https://api.github.com/repos/example/repo/actions/runs", "fixture")
    assert opening.call_count == 1
    assert clock[0] == 100.25


def test_artifact_redirect_does_not_get_a_fresh_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    calls = []

    def hop(url, headers, limit):
        calls.append((url, headers))
        clock[0] = 102.0
        return 302, "https://example.blob.core.windows.net/fixture", b""

    monkeypatch.setattr(fa, "_fetch", hop)
    with fa.transport.deadline_scope(101.0), pytest.raises(fa.ArtifactError, match="download_unavailable"):
        fa.download_zip("example/repo", 1, "fixture")
    assert len(calls) == 1


def test_scoped_deadline_restores_outer_scope_after_error():
    with fa.transport.deadline_scope(time.monotonic() + 30):
        outer = fa.transport.remaining()
        with pytest.raises(ValueError), fa.transport.deadline_scope(time.monotonic() + 1):
            assert fa.transport.remaining() <= 1
            raise ValueError("fixture")
        assert fa.transport.remaining() > outer - 1


@pytest.mark.parametrize("kind", ["artifact", "github"])
def test_real_trickling_peer_is_cut_at_absolute_deadline(kind):
    stopped = threading.Event()
    closed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                while not stopped.wait(0.01):
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                closed.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/fixture"
    try:
        with fa.transport.deadline_scope(time.monotonic() + 0.2), pytest.raises((TimeoutError, URLError)):
            if kind == "artifact":
                fa._fetch(url, {}, 10000)
            else:
                fk._gh(url, "fixture")
        assert closed.wait(2), "timeout must cancel the reader, not merely abandon it"
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert not thread.is_alive()


def test_real_poll_propagates_deadline_and_retains_unfinished_run(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tests.ci.flaky_artifact_fakes import FakeGitHub
    from tests.ci.test_flaky_kanban import FakeBoard
    from tests.ci.test_flaky_protocol_boundaries import ReadResponse
    import json

    fake = FakeGitHub()
    fake.simple()
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    requests = []

    def opening(req, timeout):
        requests.append((req.full_url, timeout))
        if req.full_url.endswith("/zip"):
            clock[0] = 102.0
            response = ReadResponse(b"")
            response.status = 302
            response.headers = {"Location": "https://example.blob.core.windows.net/fixture"}
            return response
        return ReadResponse(json.dumps(fake.gh(req.full_url, "fixture")).encode())

    monkeypatch.setattr(fk.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=opening))
    state = fk.load_state(tmp_path / "state.json")
    journal = fk.Journal(tmp_path / "queue.sqlite")
    try:
        log = fk.poll_github("o/r", "fixture", FakeBoard(), state, deadline=101.0, journal=journal)
        assert any("download_unavailable" in line for line in log)
        assert state["runs"] == []
        assert len(journal.remember_runs("o/r", [])) == 1
    finally:
        journal.close()
    assert all(timeout <= 1 for _, timeout in requests)
    assert not any("blob.core" in url for url, _ in requests)
    assert fa.transport.remaining() == 60  # poll restores the caller's context


@pytest.mark.parametrize("nested", [None, 200.0])
def test_nested_scope_cannot_extend_outer_deadline(monkeypatch, nested):
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    with fa.transport.deadline_scope(101.0), fa.transport.deadline_scope(nested):
        assert fa.transport.remaining() == 1


def test_github_oversize_response_is_not_parsed_or_retried(monkeypatch):
    reading = Mock(return_value=(200, None, b" " * (16 * 1024 * 1024 + 1)))
    monkeypatch.setattr(fa.transport, "read", reading)
    with pytest.raises(ValueError, match="size limit"):
        fk._gh("https://api.github.com/repos/example/repo/actions/runs", "fixture")
    reading.assert_called_once()


def test_expired_scope_never_opens_transport(monkeypatch):
    opening = Mock(return_value=io.BytesIO(b"{}"))
    monkeypatch.setattr(fk.urllib.request, "build_opener", opening)
    with fa.transport.deadline_scope(time.monotonic() - 1), pytest.raises(TimeoutError):
        fk._gh("https://api.github.com/repos/example/repo/actions/runs", "fixture")
    opening.assert_not_called()
