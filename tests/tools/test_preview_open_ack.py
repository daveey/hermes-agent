"""Opening a preview is acknowledged by exactly one renderer, never an event."""
import json

import pytest

from tools import desktop_ui
from tools import preview_tool  # register the real handler
from tools.registry import registry
from tui_gateway import server_requests as requests


def desktop_preview_tool(**args):
    return registry.dispatch("desktop_preview", args)


@pytest.fixture
def bridge(monkeypatch):
    requests.reset_for_tests()
    frames = []
    monkeypatch.setattr(requests, "_write", frames.append)
    monkeypatch.setattr(requests, "_emit", lambda *args: None)
    monkeypatch.setattr(requests, "_answerable", lambda sid: True)
    monkeypatch.setattr(requests, "_preview_client", lambda sid: None, raising=False)
    monkeypatch.setattr(desktop_ui, "_emit", lambda *args: frames.append(args))
    monkeypatch.setenv("HERMES_UI_SESSION_ID", "hidden")
    yield frames
    requests.reset_for_tests()


def test_open_without_a_window_cannot_report_success(bridge):
    result = json.loads(desktop_preview_tool(action="open", url="example.com"))
    assert result.get("success") is not True
    assert "Desktop window" in result["error"]
    assert bridge == []


@pytest.mark.parametrize("answer", [
    {"success": True, "url": "https://example.com", "label": "Demo"},
    {"success": False, "error": "mount failed"},
])
def test_open_returns_only_the_selected_renderers_ack(bridge, monkeypatch, answer):
    class Window:
        def write(self, frame):
            bridge.append(frame)
            assert requests.open_requests("hidden") == []  # never replay creation to another window
            requests.resolve_response({"id": frame["id"], "result": {"value": json.dumps(answer)}}, self)
            return True

    window = Window()
    monkeypatch.setattr(requests, "_preview_client", lambda sid: window, raising=False)
    result = json.loads(desktop_preview_tool(action="open", url="example.com", label=" Demo "))
    assert result == answer
    assert len(bridge) == 1
    assert bridge[0]["method"] == "preview.open"
    assert bridge[0]["params"] == {"session_id": "hidden", "url": "https://example.com", "label": "Demo"}
