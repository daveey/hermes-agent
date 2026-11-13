"""[daveey] t_01a1d31bf400d56c: a core-update continuation yields to a pending pause before its first model request.

hermes-hub 2026-11-13 04:21:57-04:25:20: the drainer released its pause (apply refused), a paused Desktop turn
(20261111_090650_1247e7) was continued in place at once (kind=auto_continue), and the drainer asked it to pause
again 5 s later. A pause landed only at a step boundary (a tool result after a model call), and this turn's
first boundary came 203 s after admission (turn setup with a 30 s pre_llm_call hook timeout, alternation repair,
a 21 s first request, a final-answer gate, a second request, an 11 s tool). Its admission reservation stayed
unpublished meanwhile, every apply past the 60 s cap was refused, and the release re-admitted the next paused
turn into the next window: the hub update stayed due for over 20 minutes.

A continuation re-runs a paused request: until its first model request it has done nothing the next backend's
continuation would not do again. So a turn the gateway marks ``update_pause_first_request`` asks its pause hook
before the turn starts (nothing is written: the transcript and the request are untouched) and again at the first
iteration (before the first request is spent). Any other turn keeps waiting for a real step boundary.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from tests.agent.test_step_boundary_pause import _final_response, _make_agent, _tool_response

HISTORY = [{"role": "user", "content": "run the job"}, {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c0", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "c0", "content": "ok"}]
NOTE = "[System note: Your previous turn was interrupted mid-run — it was paused at a step boundary ...]"


def _run(agent, responses, events, *, history=HISTORY, message=NOTE):
    agent.client.chat.completions.create.side_effect = responses

    def _persist(messages, *_a, **_k):
        events.append(("persist", [m.get("role") for m in messages]))

    with (
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session", side_effect=_persist),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message, conversation_history=[dict(m) for m in history])


def test_continuation_with_a_pending_pause_yields_before_the_turn_starts():
    """The pause is already requested when the continuation is dispatched: no request is sent, nothing is
    persisted, and the transcript comes back unchanged (the next continuation re-sends the same note)."""
    events: list = []
    calls: list = []
    agent = _make_agent(events)
    agent.step_boundary_pause = lambda: calls.append(1) or "core_update_pause"
    agent.update_pause_first_request = True

    result = _run(agent, [_tool_response(1), _final_response()], events)

    assert agent.client.chat.completions.create.call_count == 0
    assert calls == [1]
    assert result["interrupted"] is True and result["completed"] is False
    assert result["paused_for_update"] == "core_update_pause"
    assert result["api_calls"] == 0
    assert result["messages"] == HISTORY  # the note was never added
    assert not [e for e in events if e[0] == "persist"]


def test_continuation_paused_during_setup_yields_before_its_first_request():
    """The pause is requested after the turn started (during its setup): it lands at the first iteration,
    before the first request is spent, with the note persisted as the transcript's last row."""
    events: list = []
    requested = threading.Event()
    asked: list = []

    def _hook():
        asked.append(requested.is_set())
        if not requested.is_set():
            requested.set()  # the drainer's request lands right after the pre-turn check
            return None
        return "core_update_pause"

    agent = _make_agent(events)
    agent.step_boundary_pause = _hook
    agent.update_pause_first_request = True

    result = _run(agent, [_tool_response(1), _final_response()], events)

    assert agent.client.chat.completions.create.call_count == 0
    assert asked == [False, True]
    assert result["paused_for_update"] == "core_update_pause"
    assert result["messages"][-1]["role"] == "user"
    assert any(e[0] == "persist" and e[1][-1] == "user" for e in events)


def test_continuation_without_a_pause_runs_as_before():
    events: list = []
    agent = _make_agent(events)
    agent.step_boundary_pause = lambda: None
    agent.update_pause_first_request = True
    result = _run(agent, [_tool_response(1), _final_response()], events)
    assert agent.client.chat.completions.create.call_count == 2
    assert result["completed"] is True and not result.get("paused_for_update")


def test_ordinary_turn_still_waits_for_a_real_step_boundary():
    """A turn the gateway did not mark keeps the original rule: never consulted before its first request."""
    events: list = []
    calls: list = []
    agent = _make_agent(events)
    agent.step_boundary_pause = lambda: calls.append(1) or "core_update_pause"
    result = _run(agent, [_tool_response(1), _final_response()], events, message="do some tool work")
    assert calls == [1]  # only at the boundary after the first tool
    assert agent.client.chat.completions.create.call_count == 1
    assert result["paused_for_update"] == "core_update_pause"
    assert result["messages"][-1]["role"] == "tool"


def test_only_a_literal_true_marks_a_continuation():
    """A mock agent's auto-attribute is not the gateway's mark."""
    from unittest.mock import MagicMock

    from agent import update_pause_cancel
    agent = MagicMock()
    assert update_pause_cancel.pause_before_turn(agent, HISTORY) is None
