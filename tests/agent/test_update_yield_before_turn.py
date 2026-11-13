"""[daveey] t_01a1d49d50e4c656: a turn whose admission the claim would now hold yields before it starts.

David-MBP incident 31763 (2026-11-13 PST): the canonical gateway claimed a folded Kanban-wake batch on the
Desktop chat 20261026_235722_bef6c6 at 02:54, BEFORE the core-update hold; its agent turn then waited on the
session turn lease held by the legacy Desktop backend's own 43-minute turn. That turn parked for the update at
03:07:12.09 and released the lease; within ~100 ms the waiting gateway turn acquired it and started (03:07:12.353,
history=105) INSIDE the drain window, ran a turn-start preflight compression (deep aux model, no step boundary)
and parked only at 03:09:07, 3 s after the reclaim preflight refused "unmatched gateway work".

The claim's hold check (``session_notifier_drain.eligible``) ran once, at 02:54. The gateway now installs an
``update_yield_before_turn`` gate for a fresh claim of a claim-held class (a notifier wake): the agent asks it
after the lease is acquired and before anything of the turn runs (no request, no persistence, no turn-start
compression), and the gateway hands the admission back to its FIFO place.
"""

from __future__ import annotations

from unittest.mock import patch

from agent.update_pause_cancel import BEFORE_TURN_ATTR, UNSTARTED_KEY
from tests.agent.test_step_boundary_pause import _final_response, _make_agent

HISTORY = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]


def _run(agent, events, message="[IMPORTANT: 28 Kanban task updates arrived while this session was busy]"):
    agent.client.chat.completions.create.side_effect = [_final_response()]

    def _persist(messages, *_a, **_k):
        events.append(("persist", [m.get("role") for m in messages]))

    with (
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session", side_effect=_persist),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message, conversation_history=[dict(m) for m in HISTORY])


def test_held_claim_yields_before_the_turn_starts_without_compressing():
    events: list = []
    agent = _make_agent(events)
    # The turn-start preflight would compress (the 03:07:21 deep-aux compression): it must never be entered.
    agent.compression_enabled = True
    agent.context_compressor.should_compress_preflight.return_value = True
    asked: list = []
    vars(agent)[BEFORE_TURN_ATTR] = lambda: asked.append(1) or "core_update_pause"

    with patch.object(agent, "_compress_context") as compress:
        result = _run(agent, events)

    assert asked == [1]
    assert agent.client.chat.completions.create.call_count == 0
    assert compress.call_count == 0
    assert result["paused_for_update"] == "core_update_pause"
    assert result[UNSTARTED_KEY] is True
    assert result["interrupted"] is True and result["completed"] is False and result["api_calls"] == 0
    assert result["messages"] == HISTORY  # the wake text was never added
    assert not [e for e in events if e[0] == "persist"]


def test_open_gate_runs_the_turn_normally():
    events: list = []
    agent = _make_agent(events)
    asked: list = []
    vars(agent)[BEFORE_TURN_ATTR] = lambda: asked.append(1) or None

    result = _run(agent, events)

    assert asked == [1]
    assert result["final_response"] == "done"
    assert not result.get("paused_for_update") and UNSTARTED_KEY not in result


def test_failing_gate_never_pauses():
    events: list = []
    agent = _make_agent(events)

    def _boom():
        raise RuntimeError("fence unreadable")
    vars(agent)[BEFORE_TURN_ATTR] = _boom

    assert _run(agent, events)["final_response"] == "done"
