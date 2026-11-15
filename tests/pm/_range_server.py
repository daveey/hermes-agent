"""Shared Range-honoring loopback server for pm downloader tests.

Both test_downloader.py and test_store_resume.py exercise real downloads
through this one server instead of each re-implementing it. Behaviour is
configured with class attributes on :class:`RangeHandler`; the ``dl_server``
fixture starts one and resets its state.
"""

from __future__ import annotations

import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class RangeHandler(BaseHTTPRequestHandler):
    payloads: dict = {}
    ranges_seen: list = []           # (path, start, end) from real Range requests
    abort_after: int | None = None   # refuse bytes beyond this payload offset
    slow_per_chunk: float = 0.0      # sleep per served piece (pause tests)
    no_range: bool = False           # ignore Range, serve 200 full body
    etags: bool = True
    chunk: int = 1 << 20             # serve piece size
    # Ranged pieces reaching past this payload offset wait for ``hold`` to be
    # set, so a test can observe a transfer that is provably still incomplete
    # however the downloader's threads are scheduled.
    hold_from: int | None = None
    hold: threading.Event | None = None

    def log_message(self, *args):
        pass

    def end_headers(self):
        payload = self.payloads.get(self.path)
        if payload is not None and self.etags:
            self.send_header("ETag", '"' + hashlib.sha256(payload).hexdigest() + '"')
        super().end_headers()

    def do_GET(self):
        payload = self.payloads.get(self.path)
        if payload is None:
            self.send_error(404)
            return
        if self.no_range:
            # A server that ignores Range: 200 with the full body, even
            # when the client asked for a byte range.
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            served = 0
            while served < len(payload):
                if self.abort_after is not None and served >= self.abort_after:
                    self.connection.close()
                    return
                if self.slow_per_chunk:
                    time.sleep(self.slow_per_chunk)
                piece = payload[served:served + self.chunk]
                self.wfile.write(piece)
                self.wfile.flush()
                served += len(piece)
            return
        rng = self.headers.get("Range")
        if rng:
            spec = rng.removeprefix("bytes=")
            if spec == "0-0":
                # probe: 206 with total from Content-Range, 1 byte body
                total = len(payload)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 0-0/{total}")
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(payload[:1])
                self.wfile.flush()
                return
            start_s, end_s = spec.split("-", 1)
            start, end = int(start_s), int(end_s)
            self.ranges_seen.append((self.path, start, end))
            body = payload[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            served = 0
            while served < len(body):
                if self.abort_after is not None and start + served >= self.abort_after:
                    self.connection.close()
                    return
                if self.slow_per_chunk:
                    time.sleep(self.slow_per_chunk)
                end = served + self.chunk
                if self.abort_after is not None:
                    end = min(end, self.abort_after - start)
                piece = body[served:end]
                if self.hold_from is not None and self.hold is not None \
                        and start + served + len(piece) > self.hold_from:
                    self.hold.wait(120)
                self.wfile.write(piece)
                self.wfile.flush()
                served += len(piece)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


class RangeHTTPServer(ThreadingHTTPServer):
    # The downloader fans out eight connections before any need be accepted.
    # Threading handlers do not enlarge TCPServer's default backlog of five;
    # a descheduled acceptor can otherwise overflow it and force SYN retries.
    request_queue_size = 16


@pytest.fixture
def dl_server():
    RangeHandler.payloads = {}
    RangeHandler.ranges_seen = []
    RangeHandler.abort_after = None
    RangeHandler.slow_per_chunk = 0.0
    RangeHandler.no_range = False
    RangeHandler.etags = True
    RangeHandler.chunk = 1 << 20
    RangeHandler.hold_from = None
    RangeHandler.hold = None
    # Threaded, like any real file host: the downloader opens up to 8 range
    # connections at once. A single-threaded server (listen backlog 5)
    # served them one by one, so a paused download had to wait for every
    # queued connection to be drained in turn (plus SYN-retry backoff for
    # the overflow one) — ~3.7 s idle, and past the pause tests' 15 s poll
    # deadline on a loaded CI runner.
    server = RangeHTTPServer(("127.0.0.1", 0), RangeHandler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        if RangeHandler.hold is not None:
            RangeHandler.hold.set()
        server.shutdown()
        server.server_close()


def url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_port}{path}"
