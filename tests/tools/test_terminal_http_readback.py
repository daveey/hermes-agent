"""Native registered bounded HTTP readback: safety, environment and cancellation."""
import contextvars
import json
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools import terminal_tool  # register native terminal
from tools.interrupt import set_interrupt
from tools.registry import registry
from tools.terminal_scope import build_profile_terminal_scope, set_terminal_scope, reset_terminal_scope

pytestmark = pytest.mark.platforms("linux")


def command(url):
    return shlex.join(["/usr/bin/curl", "-q", "--noproxy", "*", "--path-as-is",
                       "--max-time", "5", "--silent", "--show-error", "--output",
                       "/dev/null", "--write-out", "%{http_code}", "--", url])


@pytest.fixture
def app(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            entered.set()
            if self.path == "/wait":
                release.wait(10)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    tokens = []

    def configure(*, deny=False, env_type="local"):
        (home / "config.yaml").write_text(
            f"terminal:\n  backend: {env_type}\n  cwd: {tmp_path}\n"
            + "approvals:\n  mode: manual\n  deny: "
            + json.dumps(["*curl*"] if deny else []) + "\ncommand_allowlist: "
            + json.dumps([command(url + "/ok"), command(url + "/wait")]) + "\n")
        tokens.append(set_terminal_scope(build_profile_terminal_scope(home)))
    configure()
    try:
        yield url, requests, entered, release, configure
    finally:
        for token in reversed(tokens):
            reset_terminal_scope(token)
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def dispatch(url, **kwargs):
    return json.loads(registry.dispatch("terminal", {
        "command": command(url), "http_readback": True, **kwargs,
    }, task_id="http-readback-test"))


def test_readback_preserves_real_user_deny(app):
    url, requests, _, _, configure = app
    configure(deny=True)
    result = dispatch(url + "/ok")
    assert result["error"] and result["status"] == "blocked", result
    assert "execution" not in result and not requests


@pytest.mark.parametrize("change", ["background", "pty", "promoted", "suffix", "glob", "remote"])
def test_readback_refuses_unsupported_before_execution(app, change):
    url, requests, _, _, configure = app
    kwargs = {
        "background": {"background": True},
        "pty": {"pty": True},
        "promoted": {"timeout": 100000},
        "suffix": {"command": command(url + "/ok") + "; printf 200"},
        "glob": {"command": command(url + "/{ok,other}")},
        "remote": {},
    }[change]
    if change == "remote":
        configure(env_type="ssh")
    result = dispatch(url + "/ok", **kwargs)
    assert result["error"] and "execution" not in result, result
    assert not requests


@pytest.mark.parametrize("stop", [False, True])
def test_readback_timeout_or_stop_reaps_child_without_provenance(app, stop):
    url, requests, entered, release, _ = app
    results = []
    ctx = contextvars.copy_context()
    worker = threading.Thread(target=lambda: ctx.run(
        lambda: results.append(dispatch(url + "/wait", timeout=2))))
    worker.start()
    try:
        assert entered.wait(5), results
        if stop:
            set_interrupt(True, worker.ident)
        worker.join(5)
        assert not worker.is_alive()
        assert results[0]["exit_code"] == (130 if stop else 124), results
        assert results[0]["error"] and "execution" not in results[0]
        assert requests == ["/wait"]
    finally:
        release.set()
        worker.join(5)
        set_interrupt(False, worker.ident)
