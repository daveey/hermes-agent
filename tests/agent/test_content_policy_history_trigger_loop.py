"""[daveey] End to end through the real turn loop: a refusal the earlier history caused is compacted and the
same message answered on the retry, instead of dropping the turn behind "just ask something else"
(chat 20261102_175427_594451). Unit cases: test_content_policy_history_trigger.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

MARKER = "TRIGGER-7f3a"


@pytest.fixture()
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.compression_enabled = False
    a.save_trajectories = False
    return a


def _reply(content=None, refusal=None):
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning=None, reasoning_content=None,
                          refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           model="test/model", usage=None, id="resp")


def _provider(**kwargs):
    if any(MARKER in str(m.get("content") or "") for m in kwargs.get("messages") or []):
        return _reply(refusal="I can't continue this conversation.")
    return _reply(content="It runs on the hub.")


def _compact(messages, system_message, **kw):
    summary = {"role": "assistant", "content": "[CONTEXT COMPACTION] we discussed where to deploy"}
    return [summary, *(kw.get("verbatim_tail") or [])], system_message


def test_history_triggered_refusal_is_answered_after_one_compaction(agent):
    agent.client.chat.completions.create.side_effect = _provider
    history = [{"role": "user", "content": f"earlier {MARKER}"}, {"role": "assistant", "content": "ok"}]
    with (
        patch.object(agent, "_compress_context", side_effect=_compact) as compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("does it run on the mini or the hub?", conversation_history=history)

    assert compress.call_count == 1
    assert agent.client.chat.completions.create.call_count == 2
    assert result.get("final_response") == "It runs on the hub."
    assert not result.get("failed")


def test_retry_refused_again_ends_turn_with_new_chat_copy_after_two_calls(agent):
    agent.client.chat.completions.create.side_effect = _provider
    history = [{"role": "user", "content": f"earlier {MARKER}"}, {"role": "assistant", "content": "ok"}]

    def _compact_keeps_trigger(messages, system_message, **kw):
        kept = {"role": "assistant", "content": f"[CONTEXT COMPACTION] still mentions {MARKER}"}
        return [kept, *(kw.get("verbatim_tail") or [])], system_message

    with (
        patch.object(agent, "_compress_context", side_effect=_compact_keeps_trigger) as compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("does it run on the mini or the hub?", conversation_history=history)

    assert compress.call_count == 1
    assert agent.client.chat.completions.create.call_count == 2
    assert result.get("failed") is True
    assert "/new" in result["final_response"] and "ask something else" not in result["final_response"]
