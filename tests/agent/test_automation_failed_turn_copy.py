"""A failed turn nobody typed takes its host's boundary copy, never "Send it again". [daveey]

Regression for t_01a1941d4c5d3042: a Kanban notice turn refused after persist closed on the user-turn text
"Your request was not processed. Send it again ..." and nothing ran it again.
"""
from types import SimpleNamespace

from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE

TURN = [{"role": "user", "content": "[Kanban] t_0123456789abcdef done"}]


def _close(monkeypatch, override, messages=TURN):
    from agent import conversation_loop as cl
    rows = []

    class DB:
        def latest_conversation_role(self, _sid):
            return "user"

    agent = SimpleNamespace(_session_db=DB(), session_id="s", _persist_disabled=False, provider="", model="",
                            _failed_turn_notice_override=override,
                            _flush_messages_to_session_db=lambda msgs: rows.extend(msgs))
    monkeypatch.setattr(cl, "_failed_turn_display_metadata", lambda *a: {})
    cl._close_durable_failed_turn(agent, {"completed": False, "failed": True, "error": "lease",
                                          "messages": list(messages)})
    return rows[-1]


def test_the_host_copy_replaces_send_it_again(monkeypatch):
    seen = []
    row = _close(monkeypatch, lambda result: seen.append(result) or "Couldn't process the notice: lease. Retrying "
                 "once at 10:00 PT.")
    assert row["content"].startswith("Couldn't process the notice") and "Send it again" not in row["content"]
    assert row["display_kind"] == FAILED_TURN_DISPLAY_KIND and seen[0]["error"] == "lease"


def test_no_copy_keeps_the_user_turn_boundary(monkeypatch):
    assert _close(monkeypatch, lambda result: None)["content"] == FAILED_TURN_NOTICE
    assert _close(monkeypatch, None)["content"] == FAILED_TURN_NOTICE


def test_a_raising_copy_keeps_the_user_turn_boundary(monkeypatch):
    def boom(result):
        raise RuntimeError("x")
    assert _close(monkeypatch, boom)["content"] == FAILED_TURN_NOTICE


def test_a_tool_that_ran_is_still_named(monkeypatch):
    ran = TURN + [{"role": "assistant", "tool_calls": [{"id": "c1"}]}, {"role": "tool", "content": "ok"}]
    row = _close(monkeypatch, lambda result: "Couldn't process the notice: lease. Gave up after one retry.", ran)
    assert "Gave up after one retry" in row["content"] and "verify their effects" in row["content"]
