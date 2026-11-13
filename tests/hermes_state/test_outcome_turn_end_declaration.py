"""A declared wait survives the turn's own turn-end refresh, and a refresh during a live turn. [daveey]

Desktop chat local-9c06dfbb… (2026-11-10/11): the agent declared ``waiting`` on its open hub card mid-turn; the
gateway's turn-end refresh ran while the turn's own routing token / lease was still held, derived ``running`` and
overwrote the declaration (``{"kind":"running"}`` carried nothing). The next refresh judged the reply alone and
painted the chat done (green) while the suite it promised was unbuilt.
"""

from __future__ import annotations

import pytest

import hermes_state_outcome as so
from hermes_state import SessionDB
from tui_gateway.turn_marker import clear_turn_marker, read_turn_marker, record_turn_start

WAIT = {"kind": "waiting", "on": [{"kind": "card", "id": "t_suite", "label": "Fleet scheduling regression suite"}]}
# A reply the heuristic (no judge) reads as a closing summary: without the declaration it derives done.
REPLY = "The suite is not built yet; the implementation card is queued on the hub and I will report when it lands."


@pytest.fixture
def db(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_activity as ksa
    monkeypatch.setattr(ksa, "_board_db_paths", list)
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [])
    ksa._reset_cache_for_tests()
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("chat", source="desktop")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()
    ksa._reset_cache_for_tests()


def _declare_then_reply(db):
    db.append_message("chat", role="user", content="is the suite built?")
    so.declare_outcome("chat", WAIT, db=db)
    db.append_message("chat", role="assistant", content=REPLY)


def test_a_refresh_during_the_live_turn_keeps_the_declaration(db):
    _declare_then_reply(db)
    home = db.db_path.parent
    record_turn_start(home, "chat", "is the suite built?")
    try:
        running = so.refresh_outcome("chat", "notice", db=db)
        assert running["kind"] == "running"
        assert running["declared"]["kind"] == "waiting"
    finally:
        clear_turn_marker(home, "chat")
    after = so.refresh_outcome("chat", "notice", db=db)
    assert (after["kind"], after["source"]) == ("waiting", "declared")
    assert after["on"][0]["id"] == "t_suite"


def test_the_turns_own_turn_end_refresh_ignores_its_unreleased_turn(db):
    _declare_then_reply(db)
    home = db.db_path.parent
    record_turn_start(home, "chat", "is the suite built?")  # the lease/token the concluding turn still holds
    try:
        own = so.turn_identity(marker=read_turn_marker(home, "chat"))  # the marker the turn recorded
        assert own
        out = so.refresh_outcome("chat", "turn_end", db=db, concluding_turn=own)
    finally:
        clear_turn_marker(home, "chat")
    assert (out["kind"], out["source"]) == ("waiting", "declared")
    assert so.get_outcome("chat", db=db)["kind"] == "waiting"


def test_without_a_declaration_running_still_carries_nothing(db):
    db.append_message("chat", role="user", content="go")
    home = db.db_path.parent
    record_turn_start(home, "chat", "go")
    try:
        out = so.refresh_outcome("chat", "notice", db=db)
    finally:
        clear_turn_marker(home, "chat")
    assert out["kind"] == "running" and "declared" not in out


def test_a_newer_user_row_still_supersedes_a_carried_declaration(db):
    _declare_then_reply(db)
    home = db.db_path.parent
    record_turn_start(home, "chat", "next")
    try:
        so.refresh_outcome("chat", "notice", db=db)
    finally:
        clear_turn_marker(home, "chat")
    import time
    db.append_message("chat", role="user", content="something else", timestamp=time.time() + 5)
    out = so.refresh_outcome("chat", "notice", db=db)
    assert out["source"] == "derived" and out["kind"] == "unresolved"


def test_both_turn_end_callers_pass_the_concluding_turn_never_a_blanket_bypass(monkeypatch):
    """Entry paths: the gateway (messaging/local Desktop runtime) and the TUI turn-end refresh."""
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(so, "refresh_outcome", lambda sid, trigger, **kw: calls.append((sid, trigger, kw)) or {})
    from gateway import turn_outcome as gw

    agent = SimpleNamespace(_last_session_turn_lease_holder="h")
    token = gw.run_outcome.set({"session_id": "chat"})
    try:
        gw.capture_run_outcome(agent)
        gw._refresh("chat", object(), gw.concluding_turn_identity())
    finally:
        gw.run_outcome.reset(token)
    assert calls[-1][1] == "turn_end" and calls[-1][2].get("concluding_turn") == frozenset({"lease:h"})
    assert "ignore_live_turn" not in calls[-1][2]

    import contextlib
    from tui_gateway import server
    from tui_gateway import turn_outcome as tui

    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda s: contextlib.nullcontext(), raising=False)
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(object()), raising=False)
    tui.refresh_turn_outcome({"session_key": "chat"}, agent, tui.concluding_turn_identity(agent))
    assert calls[-1][1] == "turn_end" and calls[-1][2].get("concluding_turn") == frozenset({"lease:h"})
    assert "ignore_live_turn" not in calls[-1][2]
    tui.refresh_turn_outcome({"session_key": "chat"})  # nothing named: nothing discounted
    assert calls[-1][2].get("concluding_turn") == frozenset()
