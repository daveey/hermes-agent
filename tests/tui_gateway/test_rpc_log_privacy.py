"""Protocol stdout is opt-in; gateway stdout is a diagnostic log sink."""
import io
import json
import logging

import pytest

from tui_gateway import server
from tui_gateway.transport import StdioTransport, bind_transport, reset_transport


def test_write_json_returns_false_on_broken_pipe(monkeypatch):
    class BrokenStdout:
        def write(self, text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            return None

    monkeypatch.setattr(server, "_stdio_is_rpc_channel", True)
    monkeypatch.setattr(server, "_real_stdout", BrokenStdout())
    assert server.write_json({"ok": True}) is False


@pytest.mark.parametrize("frame_kind", ["event", "request", "response"])
@pytest.mark.parametrize("route", ["fallback", "session", "context"])
def test_unrouted_rpc_never_reaches_gateway_log(tmp_path, monkeypatch, frame_kind, route):
    path = tmp_path / "gateway.log"
    payload = {"tool_id": "test-call", "name": "terminal", "result": "private-tool-result-marker"}
    frames = {
        "event": {"jsonrpc": "2.0", "method": "event", "params": {
            "type": "tool.complete", "session_id": "unattached", "payload": payload}},
        "request": {"jsonrpc": "2.0", "id": "srq-1", "method": "secret",
                    "params": {"session_id": "unattached", "label": "private-tool-result-marker"}},
        "response": {"jsonrpc": "2.0", "id": "1", "result": payload},
    }
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    if route == "session":
        monkeypatch.setitem(server._sessions, "unattached", {"transport": server._stdio_transport})
    token = bind_transport(server._stdio_transport if route == "context" else None)
    handler = logging.FileHandler(path)
    server.logger.addHandler(handler)
    try:
        with path.open("a") as out:
            monkeypatch.setattr(server, "_real_stdout", out)
            delivered = (server._emit("tool.complete", "unattached", payload) if frame_kind == "event"
                         else server.write_json(frames[frame_kind]))
            server.logger.error("real diagnostic remains visible")
        handler.flush()
        text = path.read_text()
        assert "private-tool-result-marker" not in text
        assert "jsonrpc" not in text
        assert "real diagnostic remains visible" in text
        assert delivered is False
    finally:
        reset_transport(token)
        server.logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize("stdio", [False, True])
@pytest.mark.parametrize("name", ["terminal", "connectors__example__lookup"])
def test_tool_completion_callback_respects_stdout_ownership(tmp_path, monkeypatch, stdio, name):
    path = tmp_path / "gateway.log"
    sid = "callback-privacy-test"
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", stdio)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True)
    monkeypatch.setattr(server, "_publish_session_control_snapshot", lambda *a, **kw: None)
    monkeypatch.setitem(server._sessions, sid, {
        "transport": server._stdio_transport,
        "tool_result_metadata": {"call-1": {}},
    })
    token = bind_transport(None)
    try:
        with path.open("w") as out:
            monkeypatch.setattr(server, "_real_stdout", out)
            server._on_tool_complete(sid, "call-1", name, {}, "private-callback-result")
        text = path.read_text()
        if stdio:
            assert json.loads(text)["params"]["payload"]["result"] == "private-callback-result"
        else:
            assert not text
    finally:
        reset_transport(token)


@pytest.mark.parametrize("route", ["stdio", "context", "session"])
def test_explicit_rpc_channels_still_deliver(monkeypatch, route):
    import threading

    out, log = io.StringIO(), io.StringIO()
    transport = StdioTransport(lambda: out, threading.Lock())
    monkeypatch.setattr(server, "_real_stdout", out if route == "stdio" else log)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", route == "stdio")
    if route == "session":
        monkeypatch.setitem(server._sessions, "privacy-test", {"transport": transport})
    token = bind_transport(transport if route == "context" else None)
    try:
        frame = {"jsonrpc": "2.0", "method": "event", "params": {
            "type": "tool.complete", "session_id": "privacy-test", "payload": {"result": "delivered"}}}
        assert server.write_json(frame) is True
        assert json.loads(out.getvalue())["params"]["payload"]["result"] == "delivered"
        assert not log.getvalue()
    finally:
        reset_transport(token)
