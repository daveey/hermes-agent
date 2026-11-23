"""[daveey] ``GET /api/sessions/{id}/messages`` with the Desktop's display-transcript header.

A long hub session's 120-row page was 1.4 MB, mostly ``reasoning_details`` (provider replay signatures) and
``reasoning_content`` repeating ``reasoning``: 1.5-2.5 s per open over the SSH tunnel. With the header the page
drops what a transcript never shows and is gzipped when the client accepts it; without it, nothing changes.
"""

import gzip
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HEADER = {"X-Hermes-Display-Transcript": "1"}
DETAILS = [{"type": "reasoning.encrypted", "data": "x" * 4000}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")

    db = SessionDB(home / "state.db")
    try:
        db.create_session(session_id="long-chat", source="desktop")
        db.append_messages_batch("long-chat", [
            {"role": "user", "content": "think hard"},
            {"role": "assistant", "content": "answer", "reasoning": "my thoughts",
             "reasoning_content": "my thoughts", "reasoning_details": DETAILS},
            {"role": "assistant", "content": "other", "reasoning_details": DETAILS},
        ])
    finally:
        db.close()

    from hermes_cli.web_routers.sessions import manage_router

    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as test_client:
        yield test_client


def _assistants(page):
    return [m for m in page["messages"] if m["role"] == "assistant"]


def test_without_the_header_the_page_is_unchanged(client):
    resp = client.get("/api/sessions/long-chat/messages?limit=10&order=latest", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") != "gzip"
    first, second = _assistants(resp.json())
    assert first["reasoning"] == "my thoughts" and first["reasoning_content"] == "my thoughts"
    assert first["reasoning_details"] and second["reasoning_details"]


def test_display_page_drops_replay_data_but_keeps_what_the_transcript_shows(client):
    resp = client.get("/api/sessions/long-chat/messages?limit=10&order=latest",
                      headers={**HEADER, "Accept-Encoding": "identity"})
    assert resp.headers.get("content-encoding") != "gzip"
    first, second = _assistants(resp.json())
    assert first["reasoning"] == "my thoughts" and first["content"] == "answer"
    assert "reasoning_details" not in first and "reasoning_content" not in first
    # No `reasoning` text: the Desktop falls back to reasoning_details, so it stays.
    assert second["reasoning_details"]


def test_display_page_is_gzipped_when_accepted(client):
    resp = client.get("/api/sessions/long-chat/messages?limit=10&order=latest",
                      headers={**HEADER, "Accept-Encoding": "gzip"})
    assert resp.headers["content-encoding"] == "gzip"
    page = resp.json()  # the test client decodes it
    assert page["session_id"] == "long-chat" and page["pagination"]["returned"] == 3
    assert "reasoning_details" not in _assistants(page)[0]


def test_large_valid_page_has_exact_identity_recovery():
    from starlette.requests import Request

    from hermes_cli.web_routers.sessions import _display_transcript_response

    page = {"messages": [{"role": "assistant", "content": "x" * 90_000} for _ in range(200)]}
    def request(encoding):
        return Request({"type": "http", "headers": [(b"accept-encoding", encoding)]})

    zipped = _display_transcript_response(request(b"gzip"), page)
    raw = gzip.decompress(zipped.body)
    assert len(raw) > 16 * 1024 * 1024
    assert json.loads(raw) == page
    assert _display_transcript_response(request(b"identity"), page) == page

