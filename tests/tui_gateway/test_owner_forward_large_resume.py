"""A forwarded Desktop prompt reaches the owner even when the chat's history is large.

The relay attaches with ``session.resume``, whose answer carries the whole transcript. On the hub a
long chat's answer was 1.2-1.75 MB, past websockets' 1 MiB default frame cap: the client closed with
1009 and every Desktop send to that busy chat was refused as "the gateway refused it". This drives
the real ``connect_owner`` over a real loopback WebSocket, with only the ticket call stubbed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest


def _serve_owner(resume_bytes: int):
    """A loopback owner answering ``session.resume`` with ``resume_bytes`` of history."""
    from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL
    from websockets.asyncio.server import serve

    ready, stop = threading.Event(), None
    state: dict = {}

    async def handler(ws):
        async for raw in ws:
            frame = json.loads(raw)
            method, rid = frame.get("method"), frame.get("id")
            if method == "session.resume":
                result = {"session_id": "live-owner", "messages": [{"role": "user", "content": "x" * resume_bytes}]}
            elif method == "prompt.submit":
                state["submitted"] = frame["params"]
                result = {"status": "queued", "admission_id": "adm-1"}
            else:
                result = {}
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))
            if method == "prompt.submit":
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                    "type": "message.complete", "admission_id": "adm-1", "payload": {"text": "done"}}}))

    def run():
        nonlocal stop
        loop = asyncio.new_event_loop()
        stop = loop.create_future()

        async def main():
            async with serve(handler, "127.0.0.1", 0, subprotocols=[GATEWAY_WS_PROTOCOL], max_size=None) as server:
                state["port"] = server.sockets[0].getsockname()[1]
                ready.set()
                await stop
        loop.run_until_complete(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(10)
    return state, thread, lambda: stop.get_loop().call_soon_threadsafe(stop.set_result, None)


@pytest.mark.parametrize("resume_bytes", [64 * 1024, 2 * 1024 * 1024])
def test_a_long_chat_history_does_not_refuse_the_forwarded_prompt(monkeypatch, tmp_path, resume_bytes):
    from hermes_cli import gateway_client
    from tui_gateway import owner_forward

    state, thread, shutdown = _serve_owner(resume_bytes)
    try:
        endpoint = SimpleNamespace(api_origin=f"http://127.0.0.1:{state['port']}")
        monkeypatch.setattr(gateway_client, "_session_ticket", lambda *_a, **_k: "ticket")
        monkeypatch.setattr(owner_forward, "discover_owner", lambda _home: endpoint)
        emitted = []
        session = {"session_key": "stored-key"}
        receipt = owner_forward.forward_prompt("sid", session, "Rename to Appendage.",
                                               emit=lambda *a: emitted.append(a), home=tmp_path)
        assert receipt["status"] == "queued" and receipt["admission_id"] == "adm-1"
        assert state["submitted"]["session_id"] == "live-owner"
        assert state["submitted"]["text"] == "Rename to Appendage."
    finally:
        shutdown()
        thread.join(10)
