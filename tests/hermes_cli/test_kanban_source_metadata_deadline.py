"""Metadata writes need the same interruptible deadline as selected blob writes."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_publish as pub
from hermes_cli.kanban_sync_client import Hub, HubError


@pytest.mark.parametrize("reply", ["trickle", "ok", "refused", "redirect"])
def test_private_metadata_client_bounds_response_and_never_replays(reply):
    stop = threading.Event()
    closed = threading.Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.headers.get("Content-Type"),
                          json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            status = {"ok": 200, "trickle": 200, "refused": 409, "redirect": 307}[reply]
            self.send_response(status)
            self.send_header("Content-Length", "100000" if reply == "trickle" else "2")
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            try:
                if reply == "trickle":
                    while not stop.wait(0.04):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                else:
                    self.wfile.write(b"{}")
                    self.wfile.flush()
            except OSError:
                closed.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    shared = Hub(SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", key="fixture"), timeout=5)
    hub = artifacts.bounded_transport(shared, pub._Deadline(2))
    completed = threading.Event()
    result = {}

    def invoke():
        try:
            result["value"] = hub.ops("fixture", [{"id": "one"}])
        except (OSError, HubError) as exc:
            result["error"] = exc
        finally:
            completed.set()

    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        assert completed.wait(5), "metadata trickle escaped the absolute deadline"
        if reply == "trickle":
            assert isinstance(result.get("error"), OSError)
            assert closed.wait(3), "deadline must close the reader's socket, not just abandon it"
        elif reply == "ok":
            assert result == {"value": {}}
        else:
            assert isinstance(result.get("error"), HubError)
            assert result["error"].status == (409 if reply == "refused" else 307)
        assert calls == [("/v2/boards/fixture/ops", "application/json", {"ops": [{"id": "one"}]})]
        assert shared.deadline is None
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        caller.join(5)
        hub.close()
        shared.close()
        assert not caller.is_alive() and not thread.is_alive()
