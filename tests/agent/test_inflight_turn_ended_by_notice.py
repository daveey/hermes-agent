"""A request is in flight only while its turn has not ended.

Observed 2026-11-12 (Desktop chat, core 36f9904158): a human request was answered, later turns
were started by Kanban notices (display_kind rows, never actionable), and a mid-turn compaction of
one of those notice turns restated the long-answered request after the handoff as
"STILL IN PROGRESS — this is the active request". The agent re-answered it for ~25 API calls.

``_find_inflight_user_task`` picked the newest actionable human row and only checked the
transcript END for a completed reply; a mid-turn compaction always ends on a tool result.
A completed reply followed by a turn-starting notice row now ends the earlier request's turn.
Same-turn continuations (auto_continue, scaffolding) do not.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import (
    _INFLIGHT_TASK_REPLAY_HEADER,
    _SUMMARY_END_MARKER,
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    ContextCompressor,
)

HUMAN = "HUMAN_REQUEST_sentinel_withdrew_nothing_for_the_abc_choice"
find = ContextCompressor._find_inflight_user_task


def _h() -> dict[str, Any]:
    return {"role": "user", "content": HUMAN}


def _reply(text: str = "Done: nothing was waiting on you.") -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _notice(kind: str, text: str = "✔ Kanban t_x done — review approved") -> dict[str, Any]:
    return {"role": "user", "content": text, "display_kind": kind}


def _tool_pairs(count: int, start: int = 0, *, big: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(start, start + count):
        rows.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "function": {"name": "terminal", "arguments": "{}"}}]})
        rows.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": ("tool output " * 200 if big else "ok") + f" {i}"})
    return rows


# --- T1 / T2: a notice-started turn after a completed reply ends the earlier request -------------

@pytest.mark.parametrize("kinds", [
    ("kanban_notice",),                       # T1
    ("internal_notification",),               # T2
    ("process_complete", "kanban_notice"),    # T2 variant
    ("async_delegation_complete",),
])
def test_answered_request_is_not_inflight_during_a_notice_turn(kinds):
    msgs = [{"role": "system", "content": "sys"}, _h(), _reply(),
            *[_notice(k) for k in kinds], *_tool_pairs(1)]
    assert find(msgs) is None


# --- T3 and other same-turn shapes: still in flight ---------------------------------------------

def test_auto_continue_after_reply_keeps_request_inflight():
    """T3 (unchanged): an auto_continue row resumes the same turn."""
    msgs = [_h(), _reply("partial"), _notice("auto_continue", "[System note: Your previous turn was "
                                             "interrupted mid-run]"), *_tool_pairs(1)]
    assert find(msgs) is msgs[0]


def test_scaffolding_row_after_reply_keeps_request_inflight():
    msgs = [_h(), _reply("partial"),
            {"role": "user", "content": "[Your active task list was preserved]", "_todo_snapshot_synthetic": True},
            *_tool_pairs(1)]
    assert find(msgs) is msgs[0]


def test_notice_without_a_completed_reply_does_not_end_the_turn():
    """A notice persisted mid-turn (between tool rounds) is not a new turn: no reply came first."""
    msgs = [_h(), *_tool_pairs(1), _notice("kanban_notice"), *_tool_pairs(1, start=1)]
    assert find(msgs) is msgs[0]


def test_text_bearing_tool_call_round_is_not_a_completed_reply():
    msgs = [_h(), {"role": "assistant", "content": "checking the board",
                   "tool_calls": [{"id": "c9", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c9", "content": "ok"}, _notice("kanban_notice"), *_tool_pairs(1)]
    assert find(msgs) is msgs[0]


def test_summary_carrier_is_not_a_completed_reply():
    carrier = {"role": "assistant", "content": SUMMARY_PREFIX + "\nran steps\n\n" + _SUMMARY_END_MARKER,
               COMPRESSED_SUMMARY_METADATA_KEY: True}
    msgs = [_h(), carrier, _notice("kanban_notice"), *_tool_pairs(1)]
    assert find(msgs) is msgs[0]


def test_empty_assistant_stub_after_reply_does_not_reopen_the_turn():
    msgs = [_h(), _reply(), {"role": "assistant", "content": ""}, _notice("kanban_notice"), *_tool_pairs(1)]
    assert find(msgs) is None


def test_only_user_role_notice_rows_start_a_turn():
    typed_tool = {"role": "tool", "tool_call_id": "c0", "content": "ok", "display_kind": "kanban_notice"}
    msgs = [_h(), _reply("partial"), _tool_pairs(1)[0], typed_tool]
    assert find(msgs) is msgs[0]


def test_whitespace_only_assistant_row_is_not_a_completed_reply():
    msgs = [_h(), {"role": "assistant", "content": "  \n"}, _notice("kanban_notice"), *_tool_pairs(1)]
    assert find(msgs) is msgs[0]


def test_non_dict_rows_are_skipped():
    msgs = [_h(), None, _reply(), _notice("kanban_notice"), *_tool_pairs(1)]
    assert find(msgs) is None  # type: ignore[arg-type]


def test_unknown_display_kind_after_reply_is_not_turn_starting():
    """Only the known notice kinds start a turn; anything else stays conservative (in flight)."""
    msgs = [_h(), _reply("partial"), _notice("hidden"), *_tool_pairs(1)]
    assert find(msgs) is msgs[0]


def test_merged_replay_carrier_is_not_inflight_after_its_turn_ended():
    """A carrier holding a merged restatement is the candidate too; an ended turn drops it as well."""
    carrier = {"role": "user", "content": (SUMMARY_PREFIX + "\nran steps\n\n" + _SUMMARY_END_MARKER + "\n\n"
                                           + _INFLIGHT_TASK_REPLAY_HEADER + "\n" + HUMAN),
               COMPRESSED_SUMMARY_METADATA_KEY: True}
    assert ContextCompressor._has_merged_inflight_replay(carrier)
    assert find([carrier, *_tool_pairs(1)]) is carrier
    assert find([carrier, *_tool_pairs(1), _reply(), _notice("kanban_notice"), *_tool_pairs(1, start=1)]) is None


# --- T6: the observed 04:21 shape -----------------------------------------------------------------

# state.db (MacBook) session 20261026_235722_bef6c6, rows 877967..878609 in id order: the transcript
# compaction 878610 summarised. S=summary carrier, U=human row 877982, A=assistant tool_calls,
# T=tool, R=assistant text reply, P=process_complete, C=auto_continue, K=kanban_notice.
OBSERVED_0421 = ("SATATTATTATATTRUATATATTATTATTATATATATATATATATATATTATATATTATTATTATATRPATTTTATATTTATATATATATT"
                 "TATTATTTATATTRCRKRKRKRKRKRKRKRKATTTTATATTTATATTTATATTATATTTT")


def _call_row(n: int) -> dict[str, Any]:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{n}", "function": {"name": "terminal", "arguments": "{}"}}]}


_SHAPE_ROWS = {
    "S": lambda n: {"role": "user", "content": SUMMARY_PREFIX + "\nearlier\n\n" + _SUMMARY_END_MARKER,
                    COMPRESSED_SUMMARY_METADATA_KEY: True},
    "U": lambda n: _h(),
    "A": _call_row,
    "T": lambda n: {"role": "tool", "tool_call_id": f"c{n}", "content": "ok"},
    "R": lambda n: _reply(),
    "P": lambda n: _notice("process_complete"),
    "C": lambda n: _notice("auto_continue"),
    "K": lambda n: _notice("kanban_notice"),
}


def _replay_shape(shape: str) -> list[dict[str, Any]]:
    """One row per shape letter; ``A`` opens tool call ``c<n>`` and the following ``T`` rows answer it."""
    rows: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    for n, ch in enumerate(shape):
        call = shape[: n + 1].count("A")
        rows.append(_SHAPE_ROWS[ch](call))
    return rows


def test_observed_0421_shape_is_not_inflight():
    assert find(_replay_shape(OBSERVED_0421)) is None


def test_observed_shape_before_the_answer_was_inflight():
    """Control: cut at the 03:11 compaction's view (before reply 878116) the request WAS in flight."""
    cut = OBSERVED_0421.index("U") + OBSERVED_0421[OBSERVED_0421.index("U"):].index("R")
    found = find(_replay_shape(OBSERVED_0421[:cut]))
    assert found is not None and found["content"] == HUMAN


# --- T5: end-to-end compress() mid-turn ---------------------------------------------------------

def _compress(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "## Historical Task Snapshot\nUser asked: '" + HUMAN + "'\n## Summary\nsteps."
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(model="test", quiet_mode=True, protect_first_n=2, protect_last_n=2)
    compressor.tail_token_budget = 500
    with patch("agent.context_compressor.call_llm", return_value=response):
        return compressor.compress(messages, current_tokens=200_000, force=True)


def _replays(rows: list[dict[str, Any]]) -> int:
    return sum(str(m.get("content")).count(_INFLIGHT_TASK_REPLAY_HEADER) for m in rows)


def test_compress_mid_notice_turn_does_not_restate_the_answered_request():
    out = _compress([{"role": "system", "content": "sys"}, _h(), _reply(), _notice("kanban_notice"),
                     *_tool_pairs(40, big=True)])
    assert any(ContextCompressor._is_context_summary_message(m) for m in out)
    assert _replays(out) == 0


def test_compress_mid_continued_turn_still_restates_the_request():
    """Control for T5: the same shape with an auto_continue row is still restated."""
    out = _compress([{"role": "system", "content": "sys"}, _h(), _reply("partial"),
                     _notice("auto_continue", "[System note: Your previous turn was interrupted mid-run]"),
                     *_tool_pairs(40, big=True)])
    assert _replays(out) == 1
