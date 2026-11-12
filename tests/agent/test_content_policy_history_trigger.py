"""[daveey] A safety refusal on the turn's FIRST call, with no tool round, was caused by the earlier
history, not the new message (chat 20261102_175427_594451: three harmless questions refused in a row).

Dropping only the refused turn resends the trigger, so every later message is refused too. The handler
must compact the earlier history once and retry the same message once; if the summary or the retry is
refused, it must say the history is the cause and point at /new instead of "just ask something else".
"""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.turn_retry_state import TurnRetryState
from agent.turn_truncation import handle_content_policy_refusal

MARKER = "TRIGGER-7f3a"


def _provider_refuses(messages) -> bool:
    """Fake provider: refuses any request whose history contains the marker from an earlier turn."""
    return any(MARKER in str(m.get("content") or "") for m in messages)


def _agent(*, summary_refused=False):
    from agent.transports import get_transport

    agent = MagicMock()
    agent.api_mode, agent.provider, agent.model, agent.log_prefix = "anthropic_messages", "anthropic", "claude", ""
    agent._is_anthropic_oauth = False
    agent._get_transport.return_value = get_transport("anthropic_messages")
    agent._has_pending_fallback.return_value = False
    agent._try_activate_fallback.return_value = False
    agent._extract_reasoning.return_value = ""
    agent._session_db = None
    agent._compression_skipped_due_to_lock = None
    agent._history_refusal_retry_turn = None
    agent.context_compressor = SimpleNamespace(protect_first_n=3, protect_last_n=20, tail_token_budget=4000)

    def _compress(messages, system_message, **kw):
        if summary_refused:  # the summarizer was refused too: input comes back unchanged
            return messages, "sys"
        tail = list(kw.get("verbatim_tail") or [])
        summary = {"role": "assistant", "content": "[CONTEXT COMPACTION] earlier discussion about deploy targets"}
        return [summary, *tail] if tail else [summary], "sys"

    agent._compress_context.side_effect = _compress
    return agent


def _refusal_response():
    return SimpleNamespace(
        content=[], stop_reason="refusal", usage=None, model="claude",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "duplicating model outputs"})


def _messages():
    return [
        {"role": "user", "content": f"earlier ask {MARKER}"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "should we do it on the mini or the hub?"},
    ]


def _call(agent, messages, idx=2, history=None, retry=None):
    return handle_content_policy_refusal(
        agent, _refusal_response(), retry or TurnRetryState(), thinking_spinner=None, messages=messages,
        api_messages=list(messages), api_kwargs={}, active_system_prompt=None,
        conversation_history=history if history is not None else messages[:idx],
        api_call_count=1, effective_task_id="t", turn_id="turn-1", api_request_id="r",
        api_start_time=0.0, retry_count=0, max_retries=3, current_turn_user_idx=idx,
    )


def test_history_triggered_refusal_compacts_and_retries_the_same_message(caplog):
    messages = _messages()
    assert _provider_refuses(messages)  # today's resend after dropping the turn still carries it
    agent = _agent()
    retry = TurnRetryState()
    with caplog.at_level(logging.INFO):
        verdict = _call(agent, messages, retry=retry)

    # Leave the retry loop with the compressed-restart armed: the loop rebuilds the request and resends.
    assert verdict.action == "break" and verdict.result is None
    assert retry.restart_with_compressed_messages is True
    assert agent._compress_context.call_count == 1
    kw = agent._compress_context.call_args.kwargs
    assert kw.get("force") is True and kw.get("trigger") == "content_policy_history"
    # The earlier history is summarized; the current message is retried verbatim, last.
    assert verdict.messages[-1]["content"] == "should we do it on the mini or the hub?"
    assert not _provider_refuses(verdict.messages)  # the retry answers
    assert verdict.conversation_history == verdict.messages[:-1]
    assert "history-triggered refusal: compacted+retried" in caplog.text


def test_summary_refused_closes_turn_with_history_copy_and_no_retry(caplog):
    agent = _agent(summary_refused=True)
    with caplog.at_level(logging.INFO):
        verdict = _call(agent, _messages())

    assert verdict.action == "return"
    assert agent._compress_context.call_count == 1
    text = verdict.result["final_response"]
    assert "earlier history" in text and "/new" in text
    assert "ask something else" not in text
    assert "outcome=summary_refused" in caplog.text


def test_retry_refused_again_does_not_compact_twice(caplog):
    agent = _agent()
    first = _call(agent, _messages())
    assert first.action == "break" and first.messages is not None
    # The retried request is refused as well (the trigger survived in the kept tail).
    with caplog.at_level(logging.INFO):
        second = _call(agent, first.messages, idx=len(first.messages) - 1, history=first.messages[:-1])
    assert second.action == "return"
    assert agent._compress_context.call_count == 1
    assert "/new" in second.result["final_response"]
    assert "ask something else" not in second.result["final_response"]
    assert "outcome=refused" in caplog.text


def test_caller_system_message_passes_through_and_returned_prompt_is_adopted():
    """A normal turn's caller system_message is None. Handing the full built prompt to _compress_context
    as the caller's message doubled the rebuilt prompt (review of PR #1300)."""
    agent = _agent()
    verdict = handle_content_policy_refusal(
        agent, _refusal_response(), TurnRetryState(), thinking_spinner=None, messages=_messages(),
        api_messages=_messages(), api_kwargs={}, active_system_prompt="FULL BUILT PROMPT",
        conversation_history=_messages()[:2], api_call_count=1, effective_task_id="t", turn_id="turn-1",
        api_request_id="r", api_start_time=0.0, retry_count=0, max_retries=3, current_turn_user_idx=2,
    )
    assert agent._compress_context.call_args.args[1] is None
    assert verdict.active_system_prompt == "sys"


def test_tool_round_refusal_after_the_retry_keeps_the_tool_round_copy():
    agent = _agent()
    first = _call(agent, _messages())
    assert first.action == "break"
    after_tools = first.messages + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
         "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ran"},
    ]
    second = _call(agent, after_tools, idx=len(first.messages) - 1, history=first.messages[:-1])
    assert second.action == "return"
    assert agent._compress_context.call_count == 1
    assert "ask something else" in second.result["final_response"]
    assert "didn't get past it" not in second.result["final_response"]


def test_refusal_after_a_tool_round_keeps_the_drop_and_notice_path():
    messages = _messages() + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
         "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ran"},
    ]
    agent = _agent()
    verdict = _call(agent, messages)
    assert verdict.action == "return"
    agent._compress_context.assert_not_called()
    assert "ask something else" in verdict.result["final_response"]


def test_first_message_of_a_chat_has_no_history_to_blame():
    agent = _agent()
    messages = [{"role": "user", "content": "hello"}]
    verdict = _call(agent, messages, idx=0, history=[])
    assert verdict.action == "return"
    agent._compress_context.assert_not_called()
