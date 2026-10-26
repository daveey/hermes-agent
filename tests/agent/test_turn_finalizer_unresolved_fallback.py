"""A confirmed unfinished goal must survive abnormal continuation exits honestly."""

from unittest.mock import Mock, patch

import pytest

from agent.pre_final_gate import FLAG, UnresolvedFallback
from agent.conversation_loop import finalize_turn
from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent


@pytest.mark.parametrize("exit_reason,failed,interrupted", [
    ("unknown", False, False),
    ("all_retries_exhausted_no_response", False, False),
    ("provider_failure", True, False),
    ("interrupted_by_user", False, True),
])
def test_unresolved_continuation_exit_preserves_stop_precedence(exit_reason, failed, interrupted):
    agent = _LimitAgent(budget_remaining=5)
    agent._flush_final_gate_buffer = Mock()
    agent._discard_final_gate_buffer = Mock()
    messages = [
        {"role": "user", "content": "Repair example"},
        {"role": "assistant", "content": "Rejected diagnosis.", FLAG: True},
        {"role": "user", "content": "Repair it.", FLAG: True},
    ]
    original = "Stopped by user." if interrupted else "Provider unavailable."
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = finalize_turn(
            agent, final_response=original, api_call_count=2, interrupted=interrupted,
            failed=failed, messages=messages, conversation_history=[], effective_task_id="task",
            turn_id="turn", user_message="Repair example", original_user_message="Repair example",
            _should_review_memory=False, _turn_exit_reason=exit_reason,
            _pending_verification_response=UnresolvedFallback("Repair example", ("tool:probe",)),
        )
    assert result["turn_exit_reason"] == exit_reason
    assert not result["completed"]
    assert result["interrupted"] is interrupted
    assert not agent._handle_max_iterations_called
    assert "Rejected diagnosis" not in str(result["messages"])
    if interrupted:
        assert result["final_response"] == original
    else:
        assert "Unresolved goal: Repair example" in result["final_response"]
        assert "continuation unavailable" in result["final_response"]
        assert original in result["final_response"]
        assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
        assert result["messages"][-1]["content"] == result["final_response"]
        agent._discard_final_gate_buffer.assert_called_once()
        agent._flush_final_gate_buffer.assert_not_called()
