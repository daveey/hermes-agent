"""hermes_cli.ws_logging: handled websockets failures become one INFO line; everything else keeps its level."""

from __future__ import annotations

import logging
import socket
import threading

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect
from websockets.sync.server import serve

from hermes_cli.ws_logging import ws_logger
from tests.waiting import wait_until


def _raise_and_log(log, msg):
    try:
        raise TimeoutError("timed out while closing connection")
    except TimeoutError:
        log.error(msg, exc_info=True)


def test_keepalive_failure_is_one_info_line_naming_the_cause(caplog):
    caplog.set_level(logging.DEBUG, logger="websockets.client")
    _raise_and_log(ws_logger(), "keepalive ping failed")
    [rec] = caplog.records
    assert rec.name == "websockets.client" and rec.levelno == logging.INFO and not rec.exc_info
    assert "keepalive ping failed (TimeoutError: timed out while closing connection)" in rec.getMessage()


def test_other_errors_keep_their_level_and_traceback(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="websockets")  # hermes_logging pins it at WARNING
    _raise_and_log(ws_logger("server"), "unexpected internal error")
    log = ws_logger()
    log.warning("keepalive ping failed")  # below ERROR: untouched
    log.debug("% sent keepalive ping")
    recs = [(r.name, r.levelno, bool(r.exc_info)) for r in caplog.records]
    assert recs == [("websockets.server", logging.ERROR, True), ("websockets.client", logging.WARNING, False),
                    ("websockets.client", logging.DEBUG, False)]


def test_level_config_of_the_library_logger_still_applies(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    try:
        log = ws_logger()
        assert not log.isEnabledFor(logging.INFO)  # websockets reads this to decide on debug output
        _raise_and_log(log, "keepalive ping failed")
        assert caplog.records == []  # demoted to INFO, below the library logger's WARNING floor
    finally:
        logging.getLogger("websockets.client").setLevel(logging.NOTSET)


class _StalledRecvSocket(socket.socket):
    """A client socket whose reader is descheduled right after it reads the handshake response (a loaded host):
    it holds those bytes until ``released`` is set. The test sets it once connect() has given up on
    open_timeout and closed the stream (close_socket; connect() joins this reader before raising), so the late response is
    fed into the already-closed stream on every run."""

    def __init__(self, *args):
        super().__init__(*args)
        self.released = threading.Event()

    def recv(self, n, *args):
        data = super().recv(n, *args)
        if data:
            self.released.wait(timeout=10)
        return data


def test_handshake_timeout_racing_the_response_logs_no_error(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="websockets")  # hermes_logging pins it at WARNING
    socks = []
    close_socket = ClientConnection.close_socket

    def close_then_release(self):
        close_socket(self)
        for s in socks:
            s.released.set()

    monkeypatch.setattr(ClientConnection, "close_socket", close_then_release)

    def handler(ws):
        try:
            ws.recv()
        except ConnectionClosed:
            pass

    with serve(handler, "127.0.0.1", 0, logger=ws_logger("server")) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        sock = _StalledRecvSocket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", port))
        socks.append(sock)
        with pytest.raises(TimeoutError):
            connect(f"ws://127.0.0.1:{port}", sock=sock, open_timeout=0.2, logger=ws_logger())
        wait_until(lambda: any(r.getMessage().startswith("unexpected internal error") for r in caplog.records),
                   what="the reader thread's late-response record")
        server.shutdown()
    errors = [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []
    [rec] = [r for r in caplog.records if r.getMessage().startswith("unexpected internal error")]
    assert rec.levelno == logging.INFO and not rec.exc_info and "EOFError: stream ended" in rec.getMessage()


def test_internal_error_from_another_exception_keeps_error_and_traceback(caplog):
    caplog.set_level(logging.DEBUG, logger="websockets")
    for exc in (ValueError("bad frame"), EOFError("something else")):
        try:
            raise exc
        except Exception:
            ws_logger().error("unexpected internal error", exc_info=True)
    assert [(r.levelno, type(r.exc_info[1]).__name__) for r in caplog.records] == [
        (logging.ERROR, "ValueError"), (logging.ERROR, "EOFError")]
