"""/reload-mcp on the shared gateway: the gateway reconciles its MCP servers and refreshes its
agents (``reload.mcp``); the CLI never touches its GatewayAgent stand-in's tools."""
import asyncio
from contextlib import asynccontextmanager

from cli import HermesCLI
from hermes_cli.gateway_connection import GatewayConnection


class _Client:
    def __init__(self):
        self.events = asyncio.Queue()
        self.calls = []

    async def rpc(self, method, *, _timeout=None, **params):
        self.calls.append((method, params))
        if method == "reload.mcp":
            return {"status": "reloaded", "message": "MCP servers reconciled (no server changes)"}
        if method == "session.info":
            return {"tool_count": 20, "tools": {"mcp-docs": ["docs_search"]}}
        # A loaded gateway's resume reply: the pending shape (gateway/session_local_inventory.reply_info).
        return {"revision": 2, "execution_generation": 1, "messages": [],
                "info": {"tool_count": 0, "tools": {}, "inventory_pending": True}}


def test_reload_mcp_runs_on_the_gateway(capsys):
    client = _Client()

    @asynccontextmanager
    async def connect():
        yield client
    conn = GatewayConnection(connect=connect)
    conn.start(5)
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "local-mcp"
    cli._command_running = False
    try:
        cli._reload_gateway_mcp(conn)
        state = conn.state("local-mcp").info
    finally:
        conn.close()
    assert client.calls[0] == ("reload.mcp", {"session_id": "local-mcp", "confirm": True})
    # The count comes from the full session.info, never a resume reply that can be the pending shape.
    assert [method for method, _ in client.calls[1:]] == ["session.info"]
    out = capsys.readouterr().out
    assert "20 tool(s) available" in out and "MCP reload failed" not in out, out
    assert state["tools"] == {"mcp-docs": ["docs_search"]}


class _SlowInfoClient(_Client):
    """``session.info`` runs the unbudgeted inventory build (50-84 s on a loaded host): its RPC
    deadline expires as ``GatewayClient.rpc``'s ``asyncio.wait_for`` does, with a TimeoutError."""

    async def rpc(self, method, *, _timeout=None, **params):
        if method == "session.info":
            self.calls.append((method, params))
            raise TimeoutError
        return await super().rpc(method, _timeout=_timeout, **params)


class _FailedReloadClient(_Client):
    async def rpc(self, method, *, _timeout=None, **params):
        if method == "reload.mcp":
            self.calls.append((method, params))
            raise TimeoutError
        return await super().rpc(method, _timeout=_timeout, **params)


def _run_reload(client):
    @asynccontextmanager
    async def connect():
        yield client
    conn = GatewayConnection(connect=connect)
    conn.start(5)
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "local-mcp"
    cli._command_running = False
    try:
        cli._reload_gateway_mcp(conn)
        return conn.state("local-mcp").info
    finally:
        conn.close()


def test_reload_mcp_count_read_timeout_is_success_with_unknown_count(capsys):
    client = _SlowInfoClient()
    state = _run_reload(client)
    assert [method for method, _ in client.calls] == ["reload.mcp", "session.info"]
    out = capsys.readouterr().out
    # The reload succeeded; only the count read timed out: success, count unknown, never "failed".
    assert "MCP servers reconciled (no server changes)" in out, out
    assert "MCP reload failed" not in out, out
    assert "tool count unknown" in out and "Gateway did not answer session.info" in out, out
    assert "tool(s) available" not in out, out
    assert not state.get("tools"), state


def test_reload_mcp_failure_is_still_reported(capsys):
    client = _FailedReloadClient()
    _run_reload(client)
    # A failed reload never goes on to read the count.
    assert [method for method, _ in client.calls] == ["reload.mcp"]
    out = capsys.readouterr().out
    assert "MCP reload failed" in out and "tool count unknown" not in out, out
