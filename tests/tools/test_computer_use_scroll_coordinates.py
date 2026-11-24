"""Scroll coordinate negotiation follows live tools/list, not driver versions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.computer_use.cua_backend import CuaDriverBackend
from tools.computer_use.cua_backend_session import _AsyncBridge, _CuaDriverSession


@pytest.mark.parametrize(
    "token, properties, expected_xy",
    [
        (True, {}, True),
        (False, {"x": {"type": "number"}, "y": {"type": "number"}}, True),
        (False, {}, False),
        (False, {"x": {"type": "number"}}, False),
        (False, {"y": {"type": "number"}}, False),
        (False, None, False),
        (False, [], False),
    ],
    ids=["token-only", "schema-only", "neither", "x-only", "y-only", "unknown", "malformed"],
)
def test_scroll_coordinates_follow_live_discovery(token, properties, expected_xy):
    # Missing capability metadata matches the mini's actual live tools/list.
    tool = SimpleNamespace(name="scroll", inputSchema={"properties": properties}, model_extra={})
    if token:
        tool.capabilities = ["input.scroll.coordinates"]

    class McpSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[tool], model_extra={})

    session = _CuaDriverSession(_AsyncBridge())
    asyncio.run(session._populate_capabilities(McpSession()))
    call_tool = MagicMock(return_value={"data": "ok", "images": [], "isError": False})
    session.call_tool = call_tool
    backend = CuaDriverBackend.__new__(CuaDriverBackend)
    backend._session = session
    backend._session_id = "scroll-schema-test"
    backend._snapshot_tokens = {}
    backend._active_pid = 111
    backend._active_window_id = 222

    assert backend.scroll(direction="down", x=50, y=60).ok
    name, args = call_tool.call_args.args
    assert name == "scroll"
    assert args == {
        "pid": 111, "window_id": 222, "direction": "down", "amount": 3,
        "session": backend._session_id, **({"x": 50, "y": 60} if expected_xy else {}),
    }

    # The schema fallback never authorizes unanchored window-local coordinates.
    call_tool.reset_mock()
    backend._active_window_id = None
    assert not backend.scroll(direction="down", x=50, y=60).ok
    call_tool.assert_not_called()
