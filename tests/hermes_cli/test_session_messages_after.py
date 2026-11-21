"""[daveey] ``GET /api/sessions/{id}/messages?after_id=N``: the push-driven transcript delta (``sessions.moved``).

The Desktop's transcript cache extends its cached tail with the rows after its last row id instead of refetching the
latest page. The route must return exactly the rows the latest page would show after that row, and say when the
cached run cannot be extended cleanly (the anchor row is gone, a gap, a rotation, an in-place rewrite) so the
Desktop refetches the page instead.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")

    db = SessionDB(home / "state.db")
    try:
        db.create_session(session_id="chat", source="desktop")
        db.append_messages_batch("chat", [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ])
    finally:
        db.close()

    from hermes_cli.web_routers.sessions import manage_router

    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as client:
        yield client, home / "state.db"


def _latest(client, sid="chat"):
    resp = client.get(f"/api/sessions/{sid}/messages?limit=200&order=latest&include_compacted=true")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _after(client, after_id, sid="chat", **params):
    query = "&".join([f"after_id={after_id}", *(f"{k}={v}" for k, v in params.items())])
    resp = client.get(f"/api/sessions/{sid}/messages?{query}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _append(db_path, *contents, sid="chat"):
    from hermes_state import SessionDB

    db = SessionDB(db_path)
    try:
        db.append_messages_batch(sid, [{"role": "assistant", "content": text} for text in contents])
    finally:
        db.close()


def test_delta_after_the_tail_is_empty_and_clean(env):
    client, _db = env
    tail = _latest(client)["messages"][-1]["id"]

    page = _after(client, tail)

    assert page["session_id"] == "chat"
    assert page["messages"] == []
    assert page["delta"]["anchor_found"] is True and page["delta"]["has_more"] is False
    assert isinstance(page["delta"]["transcript_revision"], int)


def test_delta_returns_exactly_the_rows_the_latest_page_shows_after_the_anchor(env):
    client, db = env
    before = _latest(client)["messages"]
    tail = before[-1]["id"]

    _append(db, "three", "four")
    delta = _after(client, tail)
    after = _latest(client)["messages"]

    assert [m["content"] for m in delta["messages"]] == ["three", "four"]
    # Cached run + delta == the fresh latest page, row for row.
    assert [m["id"] for m in before] + [m["id"] for m in delta["messages"]] == [m["id"] for m in after]
    assert [m["content"] for m in before + delta["messages"]] == [m["content"] for m in after]
    assert delta["delta"] == {**delta["delta"], "anchor_found": True, "has_more": False, "returned": 2}


def test_more_rows_than_the_limit_reports_a_gap(env):
    client, db = env
    tail = _latest(client)["messages"][-1]["id"]
    _append(db, *[f"row {n}" for n in range(5)])

    page = _after(client, tail, limit=3)

    assert [m["content"] for m in page["messages"]] == ["row 0", "row 1", "row 2"]
    assert page["delta"]["has_more"] is True


def test_a_rewound_anchor_is_not_found(env):
    client, db = env
    tail = _latest(client)["messages"][-1]["id"]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE messages SET active = 0 WHERE id = ?", (tail,))
    _append(db, "retry")

    page = _after(client, tail)

    assert page["delta"]["anchor_found"] is False
    assert page["messages"] == []


def test_an_in_place_rewrite_moves_the_transcript_revision(env):
    client, db = env
    messages = _latest(client)["messages"]
    revision = _after(client, messages[-1]["id"])["delta"]["transcript_revision"]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE messages SET content = 'edited' WHERE id = ?", (messages[0]["id"],))

    assert _after(client, messages[-1]["id"])["delta"]["transcript_revision"] == revision + 1


def test_a_compression_rotation_reads_the_child_and_misses_the_anchor(env):
    from hermes_state import SessionDB

    client, db_path = env
    tail = _latest(client)["messages"][-1]["id"]
    db = SessionDB(db_path)
    try:
        db.create_session(session_id="chat-child", source="desktop", parent_session_id="chat")
        db.append_messages_batch("chat-child", [{"role": "user", "content": "continued"}])
    finally:
        db.close()

    page = _after(client, tail)

    assert page["session_id"] == "chat-child"
    assert page["delta"]["anchor_found"] is False


def test_unknown_session_is_404(env):
    client, _db = env
    assert client.get("/api/sessions/nope/messages?after_id=1").status_code == 404
