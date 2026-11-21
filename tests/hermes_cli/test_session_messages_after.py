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


# ── review of #3912: a delta the client applies must equal a fresh page ──────────────────────────────


def _db(db_path):
    from hermes_state import SessionDB

    return SessionDB(db_path)


def _client_view(client, cached, revision, sid="chat"):
    """What the Desktop's prefetcher ends up holding: cached + delta when the delta is clean and the revision held,
    else (it refetches) the fresh latest page. Returns (view, applied_delta)."""
    page = _after(client, cached[-1]["id"], sid=sid)
    delta = page["delta"]
    clean = (delta["anchor_found"] and not delta["has_more"] and not delta["fold_conflict"]
             and page["session_id"] == sid and delta["transcript_revision"] == revision)
    if clean:
        return cached + page["messages"], True
    return _latest(client, sid)["messages"], False


def _ids_contents(messages):
    return [(m["id"], m["content"]) for m in messages]


def _revision(client, sid="chat"):
    return _after(client, _latest(client, sid)["messages"][-1]["id"], sid=sid)["delta"]["transcript_revision"]


def test_queued_prompt_drain_moves_the_revision_so_the_merge_never_differs(env):
    client, db_path = env
    db = _db(db_path)
    try:
        queued = db.append_message("chat", "user", "queued while busy")
        db.append_message("chat", "assistant", "reply to one")
        cached, revision = _latest(client)["messages"], _revision(client)
        # The drain hides the early copy and re-appends it at the end (deactivate_message).
        db.deactivate_message("chat", queued)
        db.append_message("chat", "user", "queued while busy")
    finally:
        db.close()

    view, applied = _client_view(client, cached, revision)

    assert not applied  # the hidden mid-transcript row bumped the revision
    assert _ids_contents(view) == _ids_contents(_latest(client)["messages"])


def test_model_switch_pivot_moves_the_revision_so_the_merge_never_differs(env):
    client, db_path = env
    db = _db(db_path)
    try:
        db.replace_display_kind_row("chat", "model_switch", "switched to A")
        db.append_message("chat", "assistant", "answer under A")
        cached, revision = _latest(client)["messages"], _revision(client)
        db.replace_display_kind_row("chat", "model_switch", "switched to B")
    finally:
        db.close()

    view, applied = _client_view(client, cached, revision)

    assert not applied
    assert _ids_contents(view) == _ids_contents(_latest(client)["messages"])


def test_plain_appends_still_apply_as_a_delta(env):
    client, db = env
    cached, revision = _latest(client)["messages"], _revision(client)
    _append(db, "three")

    view, applied = _client_view(client, cached, revision)

    assert applied
    assert _ids_contents(view) == _ids_contents(_latest(client)["messages"])


def test_a_row_the_page_would_fold_is_a_fold_conflict(env):
    client, db_path = env
    tail = _latest(client)["messages"][-1]["id"]
    with sqlite3.connect(db_path) as conn:
        first = conn.execute("SELECT MIN(id) FROM messages WHERE session_id = 'chat'").fetchone()[0]
        assert conn.execute("SELECT display_identity FROM messages WHERE id = ?", (first,)).fetchone()[0] is not None
        # A re-sequenced copy of an earlier row (what an in-place compaction clone leaves): same display identity.
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, display_identity, active) "
            "SELECT session_id, role, content, timestamp, display_identity, 1 FROM messages WHERE id = ?", (first,))

    assert _after(client, tail)["delta"]["fold_conflict"] is True


def test_an_ancestor_rewrite_moves_the_revision_the_tip_reports(env):
    client, db_path = env
    db = _db(db_path)
    try:
        root_rows = db.get_messages("chat")
        db.end_session("chat", "compression")
        db.create_session("chat-tip", source="desktop", parent_session_id="chat")
        db.append_message("chat-tip", root_rows[-1]["role"], root_rows[-1]["content"],
                          timestamp=root_rows[-1]["timestamp"])
        db.append_message("chat-tip", "user", "after rotation")
        assert db._resume_lineage_ids("chat-tip") == ["chat", "chat-tip"]
    finally:
        db.close()
    before = _revision(client, "chat-tip")
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE messages SET content = 'redacted' WHERE id = ?", (root_rows[0]["id"],))

    assert _revision(client, "chat-tip") == before + 1


def test_a_direct_call_without_after_id_reads_the_latest_page(monkeypatch):
    """FastAPI is not in the loop (tests and in-process callers): ``after_id``/``limit`` keep their ``Query()``
    defaults, which are not None, and must still read the latest page, never the delta."""
    import asyncio

    from starlette.requests import Request

    from hermes_cli.web_routers import sessions

    class FakeDB:
        def resolve_resume_session_id(self, sid):
            return sid

        def get_messages(self, sid, **kwargs):
            return [{"id": 1, "role": "user", "content": "hi", "timestamp": 1.0}]

        def get_display_messages_after(self, *args):  # pragma: no cover - the bug this test guards
            raise AssertionError("delta path taken for a direct call")

    monkeypatch.setattr(sessions, "_with_db", lambda profile, fn, read_only: fn(FakeDB()))
    monkeypatch.setattr(sessions, "_resolve_session_id", lambda db, sid: sid)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})

    page = asyncio.run(sessions.get_session_messages(
        request, "chat", limit=10, offset=0, order="latest", include_compacted=True, inline_images=True))

    assert "delta" not in page and page["messages"][0]["content"] == "hi"


def test_a_large_delta_is_gzipped_and_a_small_one_is_not(env):
    client, db = env
    tail = _latest(client)["messages"][-1]["id"]
    _append(db, "small")
    small = client.get(f"/api/sessions/chat/messages?after_id={tail}", headers={"Accept-Encoding": "gzip"})
    assert small.headers.get("content-encoding") != "gzip"

    _append(db, *["x" * 4000 for _ in range(8)])
    big = client.get(f"/api/sessions/chat/messages?after_id={tail}", headers={"Accept-Encoding": "gzip"})
    assert big.headers["content-encoding"] == "gzip"
    assert big.json()["delta"]["returned"] == 9
