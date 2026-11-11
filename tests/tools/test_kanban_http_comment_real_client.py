"""A real token-holding agent process (curl) posting a bare dashboard comment records agent.

Uses a real uvicorn listener built by ``build_listener_server`` (raw-transport observer
+ proxy middleware), real dashboard token middleware, real psutil socket tuples and the
real ``/proc`` ancestry reader. Socket inventory is limited to this test's children to
avoid inspecting unrelated host processes and their deleted file descriptors.
The child curl carries ``HERMES_AGENT`` in its own start-time environment, as an agent's
terminal tool does. Linux only: Darwin hides protected Apple binaries' environments.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading

import pytest

from tests.tools import test_kanban_dashboard_comment_actor as actor

worker_home = actor.worker_home
PREFIX = "/api/plugins/kanban"
TOKEN = "test-real-client-token"

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc ancestry"),
    pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl"),
]


@pytest.fixture
def served(worker_home, monkeypatch):
    from types import SimpleNamespace

    import psutil
    import uvicorn
    from fastapi import FastAPI

    def owned_connections(*, kind):
        return [SimpleNamespace(laddr=c.laddr, raddr=c.raddr, pid=p.pid)
                for p in psutil.Process().children()
                for c in p.net_connections(kind=kind)]

    monkeypatch.setattr(psutil, "net_connections", owned_connections)

    from hermes_cli import web_server
    from hermes_cli.web_server_lifecycle import build_listener_server
    from plugins.kanban.dashboard import plugin_api as api

    monkeypatch.setattr(web_server, "_SESSION_TOKEN", TOKEN)
    app = FastAPI()
    app.state.auth_required = False
    app.include_router(api.router, prefix=PREFIX)
    app.middleware("http")(web_server.auth_middleware)
    ready = threading.Event()

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config, _ = build_listener_server(app, "127.0.0.1", port, auth_required=False, lifespan="off")
        server = ReadyServer(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        try:
            assert ready.wait(10), "listener did not start"
            yield port
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "test listener failed to stop"


def _curl(port, tid, *, token, env):
    args = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
            "-H", "Content-Type: application/json", "--data", '{"body": "bare curl note"}',
            f"http://127.0.0.1:{port}{PREFIX}/tasks/{tid}/comments"]
    if token:
        args[1:1] = ["-H", f"Authorization: Bearer {token}"]
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=30, check=False).stdout


def test_agent_curl_with_token_records_agent(served):
    tid = actor._ready_task()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HERMES_AGENT": "1"}
    assert _curl(served, tid, token=TOKEN, env=env) == "200"
    assert actor._last_payload(tid)["writer_kind"] == "agent"


def test_curl_without_token_is_refused_and_stores_nothing(served):
    tid = actor._ready_task()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HERMES_AGENT": "1"}
    assert _curl(served, tid, token=None, env=env) == "401"
    assert actor._comments(tid) == []
