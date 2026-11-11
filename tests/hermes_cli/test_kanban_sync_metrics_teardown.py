"""The real deadline server must finish its own threads before interpreter exit."""
import http.server
import runpy
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tools.environments.local import build_subprocess_env


def exercise_teardown(mode):
    server_type = runpy.run_path(str(Path(__file__).with_name("test_kanban_sync_metrics_budget.py")))["Server"]
    server = server_type(mode)
    entered = threading.Event()
    handlers = []
    original = server.httpd.RequestHandlerClass.do_POST

    def tracked(handler):
        handlers.append(threading.current_thread())
        entered.set()
        original(handler)

    server.httpd.RequestHandlerClass.do_POST = tracked
    try:
        with socket.create_connection(server.httpd.server_address, timeout=5) as client:
            client.sendall(b"POST /metrics HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}")
            assert entered.wait(5), "fixture never started the handler"
            client.shutdown(socket.SHUT_RDWR)
    finally:
        server.close()
    assert handlers and not any(t.is_alive() for t in handlers), "fixture handler survived close"
    assert not server.serve_thread.is_alive(), "fixture serve loop survived close"


@pytest.mark.parametrize("mode", ["stall", "trickle"])
def test_fixture_joins_disconnected_handlers(mode, capsys):
    exercise_teardown(mode)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("mode", ["stall", "trickle"])
def test_fixture_subprocess_exits_cleanly(mode):
    result = subprocess.run(
        [sys.executable, "-m", "tests.hermes_cli.test_kanban_sync_metrics_teardown", mode],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        env=build_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("target", ["handler", "serve"])
def test_close_waits_for_owned_thread_completion(monkeypatch, target):
    server_type = runpy.run_path(str(Path(__file__).with_name("test_kanban_sync_metrics_budget.py")))["Server"]
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    close_started = threading.Event()
    owned = []
    original_serve = http.server.ThreadingHTTPServer.serve_forever

    def gated_serve(httpd):
        original_serve(httpd)
        owned.append(threading.current_thread())
        entered.set()
        release.wait(10)

    if target == "serve":
        monkeypatch.setattr(http.server.ThreadingHTTPServer, "serve_forever", gated_serve)
    server = server_type("ok")

    def gated_handler(_handler):
        owned.append(threading.current_thread())
        entered.set()
        release.wait(10)

    def close():
        close_started.set()
        try:
            server.close()
        finally:
            closed.set()

    closer = threading.Thread(target=close)
    try:
        if target == "handler":
            server.httpd.RequestHandlerClass.do_POST = gated_handler
            with socket.create_connection(server.httpd.server_address, timeout=5) as client:
                client.sendall(b"POST / HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
                assert entered.wait(5)
        closer.start()
        assert close_started.wait(5)
        assert entered.wait(5)
        assert not closed.wait(1), "close returned while its owned thread was gated"
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(10)
        else:
            server.close()
    assert closed.is_set()
    assert owned and not any(t.is_alive() for t in owned)
    assert not server.serve_thread.is_alive()


@pytest.mark.parametrize("error_type", [BrokenPipeError, ConnectionResetError, RuntimeError])
def test_handler_only_suppresses_expected_disconnects(monkeypatch, error_type):
    server_type = runpy.run_path(str(Path(__file__).with_name("test_kanban_sync_metrics_budget.py")))["Server"]
    server = server_type("ok")
    error = error_type("controlled handler failure")

    def fail(_handler):
        raise error

    monkeypatch.setattr(http.server.BaseHTTPRequestHandler, "handle", fail)
    handler_type = server.httpd.RequestHandlerClass
    handler = object.__new__(handler_type)
    try:
        if error_type is RuntimeError:
            with pytest.raises(RuntimeError) as caught:
                handler.handle()
            assert caught.value is error
        else:
            handler.handle()
    finally:
        server.close()


if __name__ == "__main__":
    exercise_teardown(sys.argv[1])
