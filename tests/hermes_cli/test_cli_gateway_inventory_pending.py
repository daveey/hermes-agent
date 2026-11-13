"""A loaded gateway answers ``session.create`` before its banner inventory is built
(``inventory_pending``, gateway/session_local_inventory.reply_info). The classic CLI printed the
gateway-judged startup lines (``unknown_toolsets``, ``tool_availability_warnings``) only from that
reply, so under load they never showed: they now print when the late ``session.info`` settles it."""
import asyncio
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
    assert db.switch_listeners == []  # the settled wait leaves no switch hook behind either
    assert len(printed) == 3, printed


def test_two_settled_frames_reaching_the_wait_print_the_notices_once(pending_store, monkeypatch):
    """Two settled ``session.info`` deliveries can both reach the wait: each ``apply_event`` snapshots
    the listener list before it calls it, so a republish whose delivery snapshotted the list before the
    first one removed the listener still calls it. The second call must print nothing and not raise."""
    db, _client = pending_store
    _host, printed = _launch(monkeypatch, db)
    state = db.connection.state("local-new")
    assert len(state.info_listeners) == 1
    waiting = state.info_listeners[0]  # what a second delivery's snapshot of the list holds

    async def deliver_twice():
        state.apply_event("session.info", dict(LATE))  # the first settled frame: prints, ends the wait
        waiting(dict(LATE))  # the second delivery, from a snapshot taken before the removal

    asyncio.run_coroutine_threadsafe(deliver_twice(), db.connection.loop).result(10)
    assert len(printed) == 3, printed
    assert "gone_plugin" in printed[0] and printed[1:] == ["", "⚠ web: set FIRECRAWL_API_KEY"], printed
    assert state.info_listeners == [] and db.switch_listeners == []


def _settle_after_switch(db, client):
    """The first session's late settled frame, then a marker frame: the pump applies frames in order, so
    once the marker is in, every listener the settled frame reached has returned."""
    _publish(db, client, dict(LATE))
    _publish(db, client, {"running": True})
    wait_until(lambda: db.connection.state("local-new").info.get("running") is True, what="frames applied")


def test_a_switch_while_pending_never_prints_the_left_sessions_notices(pending_store, monkeypatch):
    """/resume (or /new, /branch) moves the CLI to another session before the first one's inventory
    settles: that late frame belongs to a session no longer on screen, so nothing prints."""
    db, client = pending_store
    host, printed = _launch(monkeypatch, db)
    db.attach("local-old")
    _settle_after_switch(db, client)
    assert printed == [], printed
    assert host.enabled_toolsets is None  # nor is the left session's toolset list adopted


def test_the_wait_is_dropped_when_the_cli_switches_session(pending_store, monkeypatch):
    db, _client = pending_store
    _launch(monkeypatch, db)
    first = db.connection.state("local-new")
    assert len(first.info_listeners) == 1
    db.attach("local-old")
    assert first.info_listeners == []
    assert db.switch_listeners == []


def test_re_adopting_the_open_session_is_no_switch(pending_store, monkeypatch):
    """The same snapshot adopted again (a resume's attach then adopt) keeps the wait, and it still prints."""
    db, client = pending_store
    _host, printed = _launch(monkeypatch, db)
    db.set_current(db.current)
    assert len(db.connection.state("local-new").info_listeners) == 1
    _settle_after_switch(db, client)
    assert len(printed) == 3, printed


def test_the_wait_is_dropped_when_the_pending_session_ends(pending_store, monkeypatch):
    """Quit (or /new's end of the old row) before the late inventory came: nothing stays registered."""
    db, _client = pending_store
    _launch(monkeypatch, db)
    db.end_session("local-new")
    assert db.connection.state("local-new").info_listeners == []
    assert db.switch_listeners == []


def test_a_current_moved_without_a_switch_hook_still_prints_nothing(pending_store, monkeypatch):
    """Belt and braces: the print itself checks the session is still the one on screen."""
    db, client = pending_store
    _host, printed = _launch(monkeypatch, db)
    db.current = {"stored_session_id": "local-old", "info": {}}  # moved behind the hook's back
    _settle_after_switch(db, client)
    assert printed == [], printed
    assert db.connection.state("local-new").info_listeners == []


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
