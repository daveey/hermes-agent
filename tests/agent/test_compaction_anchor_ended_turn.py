"""A request whose turn already ended is never re-anchored after a compaction.

Observed 2026-11-13 11:31 PST (MacBook, session 20261026_235722_bef6c6, core main after 65f80f4ff5): a
mid-turn compaction of a background-process turn wrote user row 913645, a copy of update-resume row
913363 (same message_uid, original 11:12:09 timestamp, no "STILL IN PROGRESS" header), between the tail's
first tool group and the next call. Row 913363 carried a 7-card Kanban batch that reply 913453 had already
answered at 11:20:53; the running turn was started by process_complete row 913464.

65f80f4ff5 made ``_find_inflight_user_task`` turn-aware, so ``_reappend_inflight_user_task`` correctly
restated nothing. The copy came from the second restoration pass, ``_ensure_compressed_has_user_turn``
(``compress_context``, after the engine): when no real user row survives the compaction it anchors the
newest real user row of the ORIGINAL transcript, with no notion of whether that row's turn has ended.
65f80f4ff5's tests drove only ``ContextCompressor.compress()``, which never runs that pass.
"""

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import (
    _SUMMARY_END_MARKER,
    COMPRESSED_SUMMARY_METADATA_KEY,
    COMPRESSION_CONTINUATION_USER_CONTENT,
    SUMMARY_PREFIX,
    ContextCompressor,
)
from agent.conversation_compression import _ensure_compressed_has_user_turn
from agent.prompt_builder import format_steer_marker

BATCH = ("[IMPORTANT: 7 Kanban task updates arrived while this session was busy. Treat them as one batch and give "
         "one consolidated response.]\n\n✔ Kanban t_BATCH_SENTINEL_01 done — review approved")
RESUME = ("[System note: Your previous turn was interrupted mid-run — it was paused at a step boundary for a Hermes "
          "update. Every tool result above is complete; continue with the next step and don't redo finished steps. "
          "The paused request was:]\n\n" + BATCH)
REPLY = "All seven notices check out against live state (11:17 PST)."
PROCESS_DONE = ("[IMPORTANT: Background process proc_467899b1fa1b completed normally (exit code 0). "
                "Command: bash ~/.hermes/cache/x.sh]")


def _calls(start: int, count: int, *, big: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(start, start + count):
        rows.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "terminal", "arguments": "{}"}}]})
        rows.append({"role": "tool", "tool_call_id": f"c{i}", "content": ("tool output " * 300 if big else "ok") + f" {i}"})
    return rows


def _staged_notice() -> dict[str, Any]:
    """The current turn's row exactly as the gateway builds it in memory (turn_context, typed at turn start)."""
    from agent.turn_context import _stage_turn_user_message

    agent = MagicMock(spec=[])
    msg, _ = _stage_turn_user_message(agent, PROCESS_DONE, None, None, None, "process_complete", None)
    assert msg["display_kind"] == "process_complete"
    return msg


def _history(*, answered: bool) -> list[dict[str, Any]]:
    """Rows 913291 (batch) .. 913453 (reply) by shape: batch notice, tool rounds, update-resume row, tool rounds,
    then the completed reply (``answered``) or nothing (the resumed turn was cut before replying)."""
    rows: list[dict[str, Any]] = [
        # The session's previous compaction handoff opens the window (production head: no human row survives).
        {"role": "user", "content": SUMMARY_PREFIX + "\nearlier work\n\n" + _SUMMARY_END_MARKER,
         COMPRESSED_SUMMARY_METADATA_KEY: True},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": BATCH, "display_kind": "kanban_notice"},
        *_calls(0, 3),
        {"role": "user", "content": RESUME, "display_kind": "auto_continue"},
        *_calls(3, 18),
    ]
    if answered:
        rows.append({"role": "assistant", "content": REPLY, "finish_reason": "stop"})
    return rows


def _batch_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-summary user rows that carry the batch (the summary itself may mention it as history)."""
    return [m for m in rows if isinstance(m, dict) and m.get("role") == "user"
            and "t_BATCH_SENTINEL_01" in str(m.get("content"))
            and not ContextCompressor._is_context_summary_message(m)]


def _agent(tmp_path, *, answered: bool):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "INFLIGHT_ANCHOR_913645"
    db.create_session(session_id, source="desktop")
    for msg in _history(answered=answered):
        db.append_message(session_id, msg["role"], msg.get("content"), tool_calls=msg.get("tool_calls"),
                          tool_call_id=msg.get("tool_call_id"), display_kind=msg.get("display_kind"),
                          finish_reason=msg.get("finish_reason"))
    live = db.get_messages_as_conversation(session_id, include_row_ids=True)
    # The process-complete turn (only when the batch was answered: otherwise the resume turn is still running).
    current = ([_staged_notice()] if answered else []) + _calls(40, 30, big=True)
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                        platform="desktop", quiet_mode=True, session_db=db, session_id=session_id,
                        skip_context_files=True, skip_memory=True)
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(model="test", quiet_mode=True, protect_first_n=1, protect_last_n=2)
    compressor.tail_token_budget = 800
    agent.context_compressor = compressor
    return db, session_id, agent, [*live, *current]


def _compress(tmp_path, *, answered: bool):
    from agent.conversation_compression import CompressionCommitFence

    db, session_id, agent, messages = _agent(tmp_path, answered=answered)
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "## Summary\nworked through the Kanban batch, then a background process."
    turn_start = PROCESS_DONE if answered else RESUME
    agent._persist_user_message_idx = next(i for i, m in enumerate(messages) if m.get("content") == turn_start)
    with patch("agent.context_compressor.call_llm", return_value=response):
        out, _ = agent._compress_context(messages, "sys", approx_tokens=200_000, commit_fence=CompressionCommitFence())
    assert any(ContextCompressor._is_context_summary_message(m) for m in out), "the compaction did not run"
    return out, db.get_messages_as_conversation(session_id)


def test_answered_batch_is_not_reanchored_in_a_later_process_complete_turn(tmp_path):
    """(a)+(b) Rows 913363 -> 913453 -> 913464 (gateway-staged), then a mid-turn compaction: no restated batch."""
    out, live = _compress(tmp_path, answered=True)
    assert _batch_rows(out) == []
    assert _batch_rows(live) == []
    # No human request is in flight: the existing no-intent placeholder stands in for the anchor.
    for view in (out, live):
        assert [m.get("content") for m in view if m.get("role") == "user"][-1] == COMPRESSION_CONTINUATION_USER_CONTENT


def test_unanswered_resumed_batch_is_still_restated(tmp_path):
    """(d) Control: the resumed turn was cut before any reply, so the batch is still the active request and
    exactly one actionable copy of it survives (kept in place or restated)."""
    out, live = _compress(tmp_path, answered=False)
    assert len(_batch_rows(out)) == 1
    assert len(_batch_rows(live)) == 1


# --- the anchor pass itself ------------------------------------------------------------------------

def _handoff() -> dict[str, Any]:
    return {"role": "user", "content": SUMMARY_PREFIX + "\nsummary\n\n" + _SUMMARY_END_MARKER,
            COMPRESSED_SUMMARY_METADATA_KEY: True}


@pytest.mark.parametrize("trigger", [
    pytest.param(lambda: _staged_notice(), id="process_complete_staged"),
    pytest.param(lambda: {"role": "user", "content": "✔ Kanban t_x done", "display_kind": "kanban_notice"},
                 id="kanban_notice"),
    pytest.param(lambda: {"role": "user", "content": "[internal] note", "display_kind": "internal_notification"},
                 id="internal_notification"),
])
def test_anchor_skips_a_request_whose_turn_ended(trigger):
    """The answered batch is never the anchor. A notice the user-row filter counts as intent (Kanban, internal)
    is the running turn's own trigger and may be anchored; a process-completion row is scaffolding, so the
    no-intent placeholder stands in."""
    row = trigger()
    original = [*_history(answered=True), row, *_calls(40, 3)]
    compressed = [_handoff(), *_calls(41, 2)]
    outcome = _ensure_compressed_has_user_turn(original, compressed)
    assert _batch_rows(compressed) == []
    anchored = [m for m in compressed if m.get("role") == "user" and not ContextCompressor._is_context_summary_message(m)]
    assert len(anchored) == 1
    if outcome == "inserted":
        assert anchored[0]["content"] == row["content"]
    else:
        assert outcome == "placeholder_appended" and row.get("display_kind") == "process_complete"
        assert anchored[0]["content"] == COMPRESSION_CONTINUATION_USER_CONTENT


def test_anchor_skips_a_steer_whose_turn_ended():
    steer = {"role": "tool", "tool_call_id": "c2", "content": "ok\n" + format_steer_marker("BATCH t_BATCH_SENTINEL_01")}
    original = [{"role": "user", "content": "go"}, *_calls(0, 2), steer, {"role": "assistant", "content": REPLY},
                {"role": "user", "content": PROCESS_DONE, "display_kind": "process_complete"}, *_calls(40, 3)]
    compressed = [_handoff(), *_calls(41, 2)]
    assert _ensure_compressed_has_user_turn(original, compressed) == "placeholder_appended"
    assert _batch_rows(compressed) == []


def test_anchor_keeps_a_request_whose_turn_is_still_running():
    """Unchanged: the resumed request (auto_continue after a partial reply is the same turn) is anchored."""
    original = [*_history(answered=False), {"role": "assistant", "content": "partial"},
                {"role": "user", "content": RESUME, "display_kind": "auto_continue"}, *_calls(40, 3)]
    compressed = [_handoff(), *_calls(41, 2)]
    assert _ensure_compressed_has_user_turn(original, compressed) == "inserted"
    assert len(_batch_rows(compressed)) == 1
