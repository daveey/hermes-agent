"""A card's home chat is only ever a real chat, never a placement placeholder. [daveey]

Incident (2026-10-29, defect kanban-69709bcf16): cards the MacBook places on hub carry the placeholder
session ``kanban-placement-macbook`` (``kanban.placement.creator_session``). A hub worker on such a card
called kanban_create; the child routed to the MacBook and ``_handle_create`` took the parent card's
``session_id`` unchecked as the child's origin chat. The MacBook then asked hub to bind the card to that
"chat", hub refused (``origin_session_missing``) and the rejection surfaced in David's Inbox. Hub's
state.db has since gained a ``kanban-placement-macbook`` row (a completion notice delivered into it), so a
"does the session row exist" check alone no longer rejects the placeholder: both checks are needed.
"""
from __future__ import annotations

import json

import pytest

PLACEHOLDER = "kanban-placement-macbook"


@pytest.fixture
def worker(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_ORIGIN_SESSION",
                "HERMES_PROFILE", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement as kp
    from hermes_state import SessionDB

    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    sent = []

    def fake_create_remote(args, *, target, origin_session, origin_profile, self_gw, cfg=None, hub_cli=None):
        sent.append({"target": target, "origin_session": origin_session})
        return {"ok": True, "request_id": "placement:hub:test", "queued_only": True}

    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "macbook", "why": "test route"})
    monkeypatch.setattr(kp, "create_remote", fake_create_remote)

    def make_worker(session_id, *, persisted):
        if persisted:
            db = SessionDB(db_path=h / "state.db")
            db.create_session(session_id=session_id, source="cli")
            db.close()
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="hub worker card", assignee="default", session_id=session_id)
            kb.claim_task(conn, tid)
        finally:
            conn.close()
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        return tid

    return make_worker, sent


def _create_child(tid):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"title": "check the MacBook Chrome profile", "assignee": "default",
                                         "body": "child placed on the MacBook"}))


@pytest.mark.parametrize("persisted", [False, True], ids=["no-session-row", "placeholder-row-exists"])
def test_worker_on_a_placeholder_card_places_a_child_with_no_origin(worker, persisted):
    make_worker, sent = worker
    tid = make_worker(PLACEHOLDER, persisted=persisted)
    out = _create_child(tid)
    assert out.get("ok") is True, out
    assert sent == [{"target": "macbook", "origin_session": None}], sent


def test_worker_whose_card_session_is_gone_places_a_child_with_no_origin(worker):
    make_worker, sent = worker
    tid = make_worker("20261021_191005_0f6f74", persisted=False)
    assert _create_child(tid).get("ok") is True
    assert sent == [{"target": "macbook", "origin_session": None}], sent


def test_worker_whose_card_has_a_real_chat_keeps_it_as_origin(worker):
    make_worker, sent = worker
    tid = make_worker("20261029_053646_1e4483", persisted=True)
    assert _create_child(tid).get("ok") is True
    assert sent == [{"target": "macbook", "origin_session": "20261029_053646_1e4483"}], sent
