"""A turn a backend restart stopped before any tool ran must not tell the user to "Send it again".

Regression for self-heal desktop-3a6213ce74: an approved For-you decision was cut off by a hub update
restart (``backend_exit``) during the model wait. The boundary row said "Your request was not processed.
Send it again", but the decision tile was already closed and the restart recovery continues the turn.
"""
from types import SimpleNamespace

from agent.turn_failure_copy import (
    FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE, RESTART_FAILED_TURN_NOTICE,
    failed_turn_notice, untyped_failed_turn_display_kind,
)

DECISION = [{"role": "user", "content": "Canonical user decision. Act only on the frozen revision below."}]


def test_failed_decision_turn_copy():
    notice = failed_turn_notice(DECISION, restarted=True)
    assert notice == RESTART_FAILED_TURN_NOTICE
    assert "Send it again" not in notice and "automatically" in notice
    # A restored transcript still types the row as a Hermes boundary, not a model reply.
    assert untyped_failed_turn_display_kind("assistant", notice) == FAILED_TURN_DISPLAY_KIND


def test_restart_after_a_tool_ran_keeps_the_partial_copy():
    ran = DECISION + [{"role": "assistant", "tool_calls": [{"id": "c1"}]}, {"role": "tool", "content": "ok"}]
    assert failed_turn_notice(ran, restarted=True) == PARTIAL_FAILED_TURN_NOTICE


def test_other_failures_keep_send_it_again():
    assert failed_turn_notice(DECISION) == FAILED_TURN_NOTICE


def test_close_durable_failed_turn_detects_backend_exit(monkeypatch):
    from agent import conversation_loop as cl
    rows = []

    class DB:
        def latest_conversation_role(self, _sid):
            return "user"

    agent = SimpleNamespace(_session_db=DB(), session_id="s", _persist_disabled=False, provider="", model="",
                            _tool_interrupt_reason="backend_exit",
                            _flush_messages_to_session_db=lambda msgs: rows.extend(msgs))
    monkeypatch.setattr(cl, "_failed_turn_display_metadata", lambda *a: {})
    cl._close_durable_failed_turn(agent, {"completed": False, "interrupted": True, "messages": list(DECISION)})
    assert rows[-1]["content"] == RESTART_FAILED_TURN_NOTICE
