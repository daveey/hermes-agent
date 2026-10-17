"""Tests for the GUI-surface ``open_preview`` tool."""

import json

import pytest

from tools import desktop_ui, open_preview_tool as op
from tui_gateway import server_requests


@pytest.fixture(autouse=True)
def _reset_emitter():
    """Each test controls the emitter; never leak one across tests."""
    desktop_ui.set_emitter(None)
    yield
    desktop_ui.set_emitter(None)




def test_emitter_failure_is_reported(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("no window")

    monkeypatch.setattr(server_requests, "send", _boom)
    assert "no window" in json.loads(op.open_preview_tool("https://x.example"))["error"]


def _capture_emits(monkeypatch):
    emitted: list = []

    def _send(event, _sid, payload, **kwargs):
        emitted.append((event, payload))
        return {"value": json.dumps({"success": True, **payload})}

    monkeypatch.setattr(server_requests, "send", _send)
    return emitted


def test_existing_directory_is_an_error_not_success(tmp_path, monkeypatch):
    """#95853: a directory must not report success while opening nothing."""
    emitted = _capture_emits(monkeypatch)
    folder = tmp_path / "Active"
    folder.mkdir()

    result = json.loads(op.open_preview_tool(str(folder)))

    assert "error" in result
    assert "director" in result["error"].lower()
    assert result.get("success") is not True
    assert emitted == []


def test_existing_file_still_opens(tmp_path, monkeypatch):
    emitted = _capture_emits(monkeypatch)
    path = tmp_path / "notes.md"
    path.write_text("hi", encoding="utf-8")

    result = json.loads(op.open_preview_tool(str(path)))

    assert result["success"] is True
    assert result["url"] == str(path)
    assert emitted == [("preview.open", {"url": str(path), "label": ""})]


def test_https_url_is_not_treated_as_a_directory(monkeypatch):
    emitted = _capture_emits(monkeypatch)
    result = json.loads(op.open_preview_tool("https://example.com/docs"))

    assert result["success"] is True
    assert emitted[0][0] == "preview.open"


def test_file_uri_directory_is_an_error(tmp_path, monkeypatch):
    emitted = _capture_emits(monkeypatch)
    folder = tmp_path / "docs"
    folder.mkdir()
    uri = folder.resolve().as_uri()

    result = json.loads(op.open_preview_tool(uri))

    assert "error" in result
    assert "director" in result["error"].lower()
    assert emitted == []


def test_missing_path_still_emits(tmp_path, monkeypatch):
    """Reject only existing directories — a missing path is the renderer's call."""
    emitted = _capture_emits(monkeypatch)
    missing = tmp_path / "no-such-folder"

    result = json.loads(op.open_preview_tool(str(missing)))

    assert result["success"] is True
    assert emitted == [("preview.open", {"url": str(missing), "label": ""})]
