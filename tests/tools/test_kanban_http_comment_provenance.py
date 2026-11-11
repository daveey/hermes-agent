"""Raw loopback comment callers must not acquire operator provenance by omission."""
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import kanban_comment_writer as ancestry
from tests.tools import test_kanban_dashboard_comment_actor as actor

worker_home = actor.worker_home
client = actor.client


@pytest.fixture(autouse=True)
def supported_platform(monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban_http_comment_writer.sys", SimpleNamespace(platform="linux"))


def _connection(pid=41001, local=("127.0.0.1", 40123), remote=("127.0.0.1", 8765)):
    return SimpleNamespace(pid=pid, laddr=local, raddr=remote)


def _raw_client(client, peer=("127.0.0.1", 40123), server=("127.0.0.1", 8765)):
    @client.app.middleware("http")
    async def transport(request, call_next):
        request.scope["hermes.raw_transport"] = (peer, server)
        return await call_next(request)
    return client


def test_bare_agent_socket_comment_records_agent(client, monkeypatch):
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [_connection()])
    monkeypatch.setattr(ancestry, "agent_ancestor", lambda pid: f"{pid}:HERMES_AGENT")
    tid = actor._ready_task()
    response = _raw_client(client).post(f"/tasks/{tid}/comments", json={"body": "agent note", "author": "desktop"})
    assert response.status_code == 200, response.text
    assert actor._last_payload(tid)["writer_kind"] == "agent"


@pytest.mark.parametrize("result", [None, "41001:HERMES_AGENT", ancestry.Inconclusive("exited")])
def test_bare_comment_under_proxy_uses_raw_peer(client, monkeypatch, result):
    from fastapi.testclient import TestClient
    from hermes_cli.web_server_lifecycle import build_listener_server

    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [_connection()])

    def classify(pid):
        assert pid == 41001
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ancestry, "agent_ancestor", classify)
    config, _ = build_listener_server(client.app, "127.0.0.1", 8765, auth_required=True)
    config.load()
    proxied = TestClient(config.loaded_app, client=("127.0.0.1", 40123))
    tid = actor._ready_task()
    response = proxied.post(f"http://127.0.0.1:8765/tasks/{tid}/comments", json={"body": "note"},
                            headers={"X-Forwarded-For": "192.0.2.99", "X-Forwarded-Proto": "https"})
    assert response.status_code == 200, response.text
    assert actor._last_payload(tid)["writer_kind"] == ("human" if result is None else "agent")


@pytest.mark.parametrize("extra, status", [
    ({"writer_kind": "agent", "writer_session": "explicit-session"}, 200),
    ({"writer_kind": "human"}, 400),
    ({"writer_session": "forged"}, 400),
])
def test_explicit_actor_never_needs_socket_lookup(client, monkeypatch, extra, status):
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: pytest.fail("explicit actor lookup"))
    tid = actor._ready_task()
    response = _raw_client(client).post(f"/tasks/{tid}/comments", json={"body": "note", **extra})
    assert response.status_code == status, response.text
    if status == 200:
        payload = actor._last_payload(tid)
        assert payload["writer_kind"] == "agent"
        assert payload["writer_session"] == "explicit-session"
    else:
        assert actor._comments(tid) == []


def test_real_http_comment_resolves_client_socket_before_proxy_rewrite(client, monkeypatch):
    import http.client
    import json
    import os
    import socket
    import threading

    import uvicorn
    from hermes_cli.web_server_lifecycle import build_listener_server

    seen = []

    def classify(pid):
        seen.append(pid)
        return f"{pid}:HERMES_AGENT"

    monkeypatch.setattr(ancestry, "agent_ancestor", classify)
    ready = threading.Event()
    proc = psutil.Process(os.getpid())
    monkeypatch.setattr(psutil, "net_connections", lambda **kw: [
        _connection(proc.pid, c.laddr, c.raddr) for c in proc.net_connections(**kw)])
    monkeypatch.setattr(psutil, "process_iter", lambda: iter([proc]))

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    tid = actor._ready_task()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config, _ = build_listener_server(client.app, "127.0.0.1", port, auth_required=True, lifespan="off")
        server = ReadyServer(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        try:
            assert ready.wait(10), "listener did not start"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("POST", f"/tasks/{tid}/comments", json.dumps({"body": "bare socket note"}),
                                   headers={"Content-Type": "application/json", "X-Forwarded-For": "192.0.2.99"})
                response = connection.getresponse()
                assert response.status == 200, response.read()
                response.read()
            finally:
                connection.close()
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "test listener failed to stop"
    assert seen == [os.getpid()]
    assert actor._last_payload(tid)["writer_kind"] == "agent"
