"""Accepted Desktop sends survive a racing durable writer and takeover/reload exactly once."""

import threading
from types import SimpleNamespace

import pytest

from agent.turn_context import _stage_turn_user_message
from agent.turn_facade_lease import admit_durable_turn_lease
from hermes_cli.active_sessions import ActiveSessionRefusal, SESSION_NOT_OWNED
from hermes_state import SessionDB
from tests.tui_gateway.test_submit_time_user_row import _flush_agent
from tui_gateway import server
from tui_gateway.prompt_turn import _TurnRun


@pytest.mark.parametrize("late", [False, True], ids=["gateway-admission", "durable-admission"])
@pytest.mark.parametrize("refresh", [False, True], ids=["warm-reload", "refreshed-reload"])
def test_takeover_retry_preserves_the_accepted_followup_once(monkeypatch, tmp_path, late, refresh):
    db = SessionDB(db_path=tmp_path / "state.db")
    key = "takeover-recovery"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (
        threading.Event(), SimpleNamespace(join=lambda: None)))
    frames = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: frames.append((event, payload)))
    agent = _flush_agent(db, key)
    agent.platform = "desktop"
    agent._emit_warning = lambda message: None
    # Equal text is deliberate: recovery must address the new send, not the older identical ask.
    prior = [{"role": "user", "content": "follow up"}, {"role": "assistant", "content": "first answer"}]
    agent._flush_messages_to_session_db(prior, [])
    session = {"agent": agent, "session_key": key, "history": prior[:], "history_version": 0,
               "history_lock": threading.Lock(), "running": True, "cols": 80}
    server._start_inflight_turn(session, "follow up")
    server._persist_submit_user_row(session, "follow up", None)
    accepted = session["_submit_user_row"]
    accepted_id = accepted["_row_id"]
    holder = "test-cli-writer"
    assert db.acquire_session_turn_lease(key, holder, ttl_seconds=300, wait_seconds=0)
    try:
        if late:
            def run_conversation(message, **kwargs):
                admission = admit_durable_turn_lease(
                    agent, session_id=key, relay_turn_id="desktop-race",
                    task_context={"platform": "desktop"}, conversation_history=kwargs["conversation_history"])
                assert admission.lease is None
                assert admission.early_result is not None
                return admission.early_result

            agent.run_conversation = run_conversation
            st = _TurnRun(agent, None, None, False, history=prior[:], history_version=0)
            server._invoke_agent("ui", session, st, "follow up", "follow up", None,
                                 [], None, None, text="follow up")
            server._absorb_turn_result("ui", session, st, "follow up", None, None)
            payload, _, status = server._complete_turn_payload(session, st, None, 80)
            assert status == "error"
            assert agent._pending_cli_user_message is None
        else:
            monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: ActiveSessionRefusal(
                "Another writer owns this chat", SESSION_NOT_OWNED))
            assert server._admit_prompt_turn("ui", session, "follow up", [], None, None, None) is None
            payload = next(payload for event, payload in frames if event == "message.complete")
            assert session["running"] is False
        assert payload["error_surface"] == {
            "layer": "gateway", "code": "SESSION_NOT_OWNED", "retryable": False}
        assert session["inflight_turn"]["error_surface"] == payload["error_surface"]
        assert [row["_row_id"] for row in session["history"]] == [
            prior[0]["_row_id"], prior[1]["_row_id"], accepted_id]
        assert len(db.get_messages_as_conversation(key)) == 3
        assert "_submit_user_row" not in session

        # session.take_over waits for release. Desktop then refreshes and reloads via rewind;
        # the failed-refresh fallback must also have the accepted row in warm history.
        db.release_session_turn_lease(key, holder)
        assert db.session_turn_lease_expires_at(key, holder) is None
        if refresh:
            session["history"] = db.get_messages_as_conversation(key, include_row_ids=True)
        with session["history_lock"]:
            prefix, live, _ = server._rewind_active_session_history(session, 1, require_retryable=True)
        assert [row["_row_id"] for row in prefix] == [row["_row_id"] for row in prior]
        assert live["content"] == "follow up"
        server._persist_submit_user_row(session, live["content"], None)
        server._adopt_submit_user_row(session, agent, live["content"], live["content"])
        user, _ = _stage_turn_user_message(agent, live["content"], live["content"], None, None, None, None)
        agent._persist_user_message_idx = len(prefix)
        agent._flush_messages_to_session_db(prefix + [user, {"role": "assistant", "content": "followup answer"}], prefix)
        rows = db.get_messages_as_conversation(key, include_row_ids=True)
        assert [(row["role"], row["content"]) for row in rows] == [
            ("user", "follow up"), ("assistant", "first answer"),
            ("user", "follow up"), ("assistant", "followup answer")]
        assert sum(row["_row_id"] == user["_row_id"] for row in rows) == 1
    finally:
        db.release_session_turn_lease(key, holder)
        db.close()
