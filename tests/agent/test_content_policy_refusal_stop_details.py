"""handle_content_policy_refusal: an Anthropic refusal with an empty body still tells the user why."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.conversation_loop import _close_durable_failed_turn
from agent.turn_failure_copy import FAILED_TURN_NOTICE, failed_turn_notice
from agent.turn_retry_state import TurnRetryState
from agent.turn_truncation import handle_content_policy_refusal


def _agent():
    import agent.transports.anthropic
    from agent.transports import get_transport

    agent = MagicMock()
    agent.api_mode, agent.provider, agent.model, agent.log_prefix = "anthropic_messages", "anthropic", "claude", ""
    agent._is_anthropic_oauth = False
    agent._get_transport.return_value = get_transport("anthropic_messages")
    agent._has_pending_fallback.return_value = False
    agent._try_activate_fallback.return_value = False
    agent._extract_reasoning.return_value = ""
    return agent


def test_empty_refusal_reports_stop_details_explanation():
    response = SimpleNamespace(
        content=[], stop_reason="refusal", usage=None, model="claude",
        stop_details={"type": "refusal", "category": "general_harms", "explanation": "classifier halt"},
    )
    verdict = handle_content_policy_refusal(
        _agent(), response, TurnRetryState(), thinking_spinner=None, messages=[], api_messages=[], api_kwargs={},
        active_system_prompt=None, conversation_history=[], api_call_count=1, effective_task_id="t", turn_id="u",
        api_request_id="r", api_start_time=0.0, retry_count=0, max_retries=0,
    )
    assert verdict.action == "return"
    assert "classifier halt" in verdict.result["error"]


@pytest.mark.parametrize("explanation", [None, "classifier halt"])
@pytest.mark.parametrize("partial", [False, True])
def test_refusal_category_reaches_visible_boundary(explanation, partial):
    agent = _agent()
    agent._persist_disabled = False
    agent._failed_turn_notice_override = None
    agent.session_id = "refusal-test"
    agent._session_db.latest_conversation_role.return_value = "tool" if partial else "user"
    messages = [{"role": "user", "content": "Why choose this host?"}]
    if partial:
        messages.append({"role": "tool", "content": "done", "tool_call_id": "call"})
    response = SimpleNamespace(
        content=[], stop_reason="refusal", usage=None, model="claude",
        stop_details={"category": "reasoning_extraction", "explanation": explanation},
    )
    verdict = handle_content_policy_refusal(
        agent, response, TurnRetryState(), thinking_spinner=None, messages=messages,
        api_messages=[], api_kwargs={}, active_system_prompt=None, conversation_history=[],
        api_call_count=1, effective_task_id="t", turn_id="u", api_request_id="r",
        api_start_time=0.0, retry_count=0, max_retries=0,
    )
    _close_durable_failed_turn(agent, verdict.result)
    boundary = messages[-1]
    assert boundary["display_kind"] == "failed_turn"
    assert "provider" in boundary["content"]
    assert "refused" in boundary["content"]
    assert "reasoning_extraction" in boundary["content"]
    assert "new chat" in boundary["content"]
    assert "rephrase" in boundary["content"]
    assert "Your request was not processed" not in boundary["content"]
    assert ("Some actions may already have run" in boundary["content"]) == partial
    agent._flush_messages_to_session_db.assert_called_once_with(messages)


def test_other_failure_keeps_generic_visible_boundary():
    agent = _agent()
    agent._persist_disabled = False
    agent._failed_turn_notice_override = None
    agent.session_id = "failure-test"
    agent._session_db.latest_conversation_role.return_value = "user"
    messages = [{"role": "user", "content": "Hello"}]
    _close_durable_failed_turn(agent, {
        "messages": messages, "completed": False, "failed": True, "failure_reason": "server_error",
    })
    assert messages[-1]["content"] == FAILED_TURN_NOTICE


@pytest.mark.parametrize("category", [None, {}, "", "x" * 81, "<script>bad</script>", "secret\ntext"])
def test_missing_or_malformed_category_still_explains_refusal(category):
    notice = failed_turn_notice([], result={
        "failure_reason": "content_policy_blocked", "refusal_category": category,
    })
    assert "provider's content filter refused" in notice
    assert "category:" not in notice
    assert "<script>" not in notice
    assert "secret" not in notice
