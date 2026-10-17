"""Preview creation picks one capable peer and preserves hidden-session routing."""
from tui_gateway import server, server_requests
from tui_gateway.transport import FanoutTransport


class Window:
    _closed = False
    auth_identity = None

    def write(self, frame):
        return True


def test_preview_owner_then_stable_fallback(monkeypatch):
    first, owner = Window(), Window()
    monkeypatch.setattr(server, "_live_transports", {first, owner})
    monkeypatch.setattr(server, "_sessions", {"hidden": {"session_key": "hidden"}, "owned": {"transport": owner}})
    monkeypatch.setattr(server_requests, "answers_requests", lambda peer: True)
    assert server._preview_request_client("owned") is owner
    chosen = server._preview_request_client("hidden")
    assert chosen in {first, owner}
    assert server._sessions["hidden"]["transport"] is chosen
    assert server._preview_request_client("hidden") is chosen
    monkeypatch.setitem(server._sessions, "owned", {"transport": FanoutTransport(owner, first)})
    assert server._preview_request_client("owned") is owner
    chosen._closed = True
    assert server._preview_request_client("hidden") is not chosen


def test_no_capable_or_same_identity_window_is_unavailable(monkeypatch):
    peer = Window()
    monkeypatch.setattr(server, "_live_transports", {peer})
    monkeypatch.setattr(server, "_sessions", {"hidden": {"auth_user_id": "basic:alice"}})
    monkeypatch.setattr(server_requests, "answers_requests", lambda peer: False)
    assert server._preview_request_client("hidden") is None
    monkeypatch.setattr(server_requests, "answers_requests", lambda peer: True)
    assert server._preview_request_client("hidden") is None
