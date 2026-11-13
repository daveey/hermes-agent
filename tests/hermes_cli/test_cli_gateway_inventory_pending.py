"""A loaded gateway answers ``session.create`` before its banner inventory is built
(``inventory_pending``, gateway/session_local_inventory.reply_info). The classic CLI printed the
gateway-judged startup lines (``unknown_toolsets``, ``tool_availability_warnings``) only from that
reply, so under load they never showed: they now print when the late ``session.info`` settles it."""
from contextlib import asynccontextmanager

import pytest

from hermes_cli.gateway_connection import GatewayConnection
from hermes_cli.gateway_session_store import GatewaySessionStore
from tests.hermes_cli.test_cli_profile_toolsets import _launch
from tests.hermes_cli.test_gateway_session_store import FakeClient
from tests.waiting import wait_until

LATE = {"inventory_pending": False, "enabled_toolsets": ["terminal"], "unknown_toolsets": ["gone_plugin"],
        "tool_availability_warnings": ["⚠ web: set FIRECRAWL_API_KEY"], "tools": {"terminal": ["terminal"]},
        "skills": {}, "mcp_servers": [], "tool_count": 1}


@pytest.fixture
def pending_store():
    client = FakeClient()
    real_rpc = client.rpc

    async def rpc(method, *, _timeout=None, **params):
        reply = await real_rpc(method, **params)
        if method == "session.create":
            info = reply["info"]
            for key in ("enabled_toolsets", "tool_availability"):
                info.pop(key, None)
            info.update(tools={}, skills={}, mcp_servers=[], inventory_pending=True)
        return reply
    client.rpc = rpc

    @asynccontextmanager
    async def connect():
        yield client
    conn = GatewayConnection(connect=connect)
    conn.start(5)
    yield GatewaySessionStore(conn), client
    conn.close()


def _publish(store, client, payload):
    frame = {"jsonrpc": "2.0", "method": "event",
             "params": {"type": "session.info", "session_id": "local-new", "payload": payload}}
    store.connection.loop.call_soon_threadsafe(client.events.put_nowait, frame)


def test_late_inventory_prints_the_startup_notices_once(pending_store, monkeypatch):
    db, client = pending_store
    host, printed = _launch(monkeypatch, db)
    assert printed == []  # pending: nothing judged yet, and no full banner of zero tools
    assert db.banner_inventory() is None
    _publish(db, client, {"running": True})  # an unrelated session.info (a turn) settles nothing
    _publish(db, client, dict(LATE))
    wait_until(lambda: len(printed) >= 3, what="late startup notices printed")
    assert "gone_plugin" in printed[0], printed
    assert printed[1:] == ["", "⚠ web: set FIRECRAWL_API_KEY"], printed
    assert host.enabled_toolsets == ["terminal"]
    inventory = db.banner_inventory()  # the snapshot now carries the settled inventory
    assert inventory is not None and inventory["tool_availability_warnings"] == LATE["tool_availability_warnings"]
    _publish(db, client, dict(LATE))  # a later settled frame (another viewer's open) prints nothing again
    wait_until(lambda: db.connection.state("local-new").info.get("running") is True, what="frames applied")
    assert db.connection.state("local-new").info_listeners == []
    assert len(printed) == 3, printed


def test_a_settled_reply_prints_at_once_and_waits_on_nothing(pending_store, monkeypatch):
    db, client = pending_store
    real_rpc = client.rpc

    async def settled(method, **params):
        reply = await real_rpc(method, **params)
        if method == "session.create":
            reply["info"].update(LATE)
        return reply
    client.rpc = settled
    _host, printed = _launch(monkeypatch, db)
    assert len(printed) == 1 and "gone_plugin" in printed[0], printed  # the banner prints the warnings
    assert db.connection.state("local-new").info_listeners == []
