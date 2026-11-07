"""Fork stop nudges remain scaffolding after the shared predicate extraction."""

import pytest

from agent.conversation_compression import _is_real_user_message
from agent.conversation_compression_scaffolding import is_user_role_scaffolding
from agent.turn_context import _previous_user_message_text


@pytest.mark.parametrize("flag", ["_pre_final_gate_synthetic", "_kanban_stop_synthetic"])
@pytest.mark.parametrize("content", ["Finish the required check", [{"type": "text", "text": "Finish the required check"}]])
def test_fork_stop_nudges_are_not_human_anchors_or_previous_asks(flag, content):
    ask = {"role": "user", "content": "Ship the verified change"}
    nudge = {"role": "user", "content": content, flag: True}
    assert is_user_role_scaffolding(nudge)
    assert not _is_real_user_message(nudge)
    assert _previous_user_message_text([ask, nudge]) == ask["content"]
    assert _previous_user_message_text([nudge]) == ""


@pytest.mark.parametrize("flag", ["_pre_final_gate_synthetic", "_kanban_stop_synthetic"])
def test_false_stop_flag_does_not_hide_a_real_user_ask(flag):
    ask = {"role": "user", "content": "Finish the required check", flag: False}
    assert not is_user_role_scaffolding(ask)
    assert _is_real_user_message(ask)
    assert _previous_user_message_text([ask]) == ask["content"]
