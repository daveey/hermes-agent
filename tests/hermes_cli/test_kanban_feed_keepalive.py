"""A Board feed socket that goes silent is a handled drop: FeedWatcher reconnects, and nothing logs at ERROR.

Regression for self-heal defect agent-ac35b4d534: websockets' sync keepalive thread logged
``ERROR websockets.client: keepalive ping failed`` + traceback when the Board stopped answering pings, one
millisecond before FeedWatcher's own INFO "feed socket closed ... reconnecting" line. errors.log then read like an
unhandled crash and self-heal filed it as a defect. Hermes now passes its own logger (``hermes_cli.ws_logging``),
so that case is one INFO line.
"""

from __future__ import annotations

import functools
import logging
import socket
import threading

import pytest

from hermes_cli import kanban_sync_client as ksc
from tests.waiting import wait_until

pytest.importorskip("websockets")


class _SilentRelay:
    """TCP relay in front of a real websocket server: forwards the HTTP upgrade both ways, then reads and drops
    every later byte, like a Board that stops answering pings without closing the socket."""

    def __init__(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._socks: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self.listener.accept()
            except OSError:  # accept timeout (TimeoutError is an OSError) or the listener closed
                continue
            self.connections += 1
            upstream = socket.create_connection(("127.0.0.1", self.upstream_port))
            self._socks += [client, upstream]
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(src, dst), daemon=True).start()

    def _pipe(self, src: socket.socket, dst: socket.socket) -> None:
        head = b""  # bytes seen so far while the HTTP upgrade is still in flight
        while not self._stop.is_set():
            try:
                data = src.recv(65536)
            except OSError:
                return
            if not data:
                return
            if head is None:
                continue  # upgraded: swallow the frame
            seen, head = len(head), head + data
            end = head.find(b"\r\n\r\n")
            upto = len(head) if end < 0 else end + 4
            try:
                dst.sendall(head[seen:upto])
            except OSError:
                return
            if end >= 0:
                head = None

    def close(self) -> None:
        self._stop.set()
        for s in [self.listener, *self._socks]:
            s.close()


def test_silent_board_socket_reconnects_without_an_error_record(monkeypatch, caplog):
    import websockets.sync.client as ws_client
    from websockets.sync.server import serve

    # Library keepalive at 0.3 s (production keeps the library's 20 s default). Every other kwarg, logger
    # included, is whatever FeedWatcher passes.
    monkeypatch.setattr(ws_client, "connect", functools.partial(ws_client.connect, ping_interval=0.3,
                                                                ping_timeout=0.3, close_timeout=0.3))
    stop = threading.Event()

    def handler(ws):
        ws.send('{"type": "hello", "head": 1}')
        while not stop.is_set():
            try:
                ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:  # the client dropped the socket; the test server just ends
                return

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="websockets.client")  # hermes_logging pins it at WARNING in production
    with serve(handler, "127.0.0.1", 0, logger=logging.getLogger("test.board_server")) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        relay = _SilentRelay(server.socket.getsockname()[1])
        ident = ksc.Identity(url=f"http://127.0.0.1:{relay.port}", key="gw-key", gateway="gw-test")
        watcher = ksc.FeedWatcher(ident, "keepalive-board", threading.Event(), lambda: not stop.is_set(), ping_s=0.5)
        t = threading.Thread(target=watcher.run, daemon=True)
        t.start()
        try:
            wait_until(lambda: relay.connections >= 2, timeout=15, what="the silent socket dropped and reconnected")
            wait_until(lambda: any("keepalive ping failed" in r.getMessage() for r in caplog.records), timeout=5,
                       what="the library's keepalive failure record")
        finally:
            stop.set()
            relay.close()
            server.shutdown()
            t.join(timeout=5)

    closed = [r for r in caplog.records if "feed socket closed" in r.getMessage()]
    assert closed and all(r.levelno == logging.INFO for r in closed), "FeedWatcher logs the drop at INFO"
    errors = [f"{r.name}: {r.getMessage()}" for r in caplog.records
              if r.levelno >= logging.ERROR and not r.name.startswith("test.")]
    assert errors == [], f"a handled keepalive drop must not log at ERROR: {errors}"
    keepalive = [r for r in caplog.records if "keepalive ping failed" in r.getMessage()]
    assert all(r.levelno == logging.INFO and not r.exc_info for r in keepalive), \
        "the keepalive failure is one INFO line without a traceback"
