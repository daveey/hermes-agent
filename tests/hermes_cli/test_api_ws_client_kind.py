"""``/api/ws?client=agent`` marks the connection's transport as an agent client; no marker = today. [daveey]

Agent scripts that drive the Desktop backend (patching-local-hermes ``gateway_rpc.py``) declare themselves
with the query param so ``session.create`` defaults to hidden and their ``prompt.submit`` is not counted as
the user typing. The marker is client-declared: it picks defaults, it is not a security boundary.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from tui_gateway import server as gateway_server

TOKEN = "c" * 64


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", TOKEN)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(web_server.app.state, "ssh_bootstrap_identity", None, raising=False)
    return TestClient(web_server.app, base_url="http://127.0.0.1")


@pytest.fixture
def seen(monkeypatch):
    """Replace the JSON-RPC dispatcher: record each request's transport ``client_kind`` and answer it."""
    kinds: list = []

    def dispatch(req, transport=None):
        kinds.append(getattr(transport, "client_kind", "<missing>"))
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    monkeypatch.setattr(gateway_server, "dispatch", dispatch)
    return kinds


def _one_rpc(client, query: str) -> None:
    with client.websocket_connect(f"/api/ws?token={TOKEN}{query}", headers={"host": "127.0.0.1"}) as ws:
        assert ws.receive_json()["params"]["type"] == "gateway.ready"
        ws.send_json({"jsonrpc": "2.0", "id": 7, "method": "session.list", "params": {}})
        reply = ws.receive_json()
        while reply.get("id") != 7:  # events may interleave before the response
            reply = ws.receive_json()
        assert "result" in reply, reply


def test_client_agent_param_sets_agent_client_kind(client, seen):
    _one_rpc(client, "&client=agent")
    assert seen == ["agent"]


@pytest.mark.parametrize("query", ["", "&client=renderer", "&client=AGENT"])
def test_without_agent_param_client_kind_is_unset(client, seen, query):
    _one_rpc(client, query)
    assert seen == [None]
