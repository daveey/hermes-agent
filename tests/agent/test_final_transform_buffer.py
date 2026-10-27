"""Buffered final text stays private through output transformation."""
from unittest.mock import patch

import pytest

from tests.agent.test_pre_final_gate import Run, _buffered, _run
from tests.agent import test_pre_final_gate as support

agent = support.agent


@pytest.mark.parametrize("directive", [None, {"append": "Additional guidance."},
                                       {"replace": "A gate replacement."}])
def test_transform_precedes_buffer_release(agent, directive):
    run = Run(agent, ["Please repeat the retired action."])
    safe = "The resolved request remains closed."
    observed = []

    def hooks(name, logger, **kwargs):
        if name == "transform_llm_output":
            observed.append(kwargs["response_text"])
            assert run.shown == ""
            return [safe]
        return []

    with patch("agent.turn_finalizer._invoke_hook_safely", side_effect=hooks):
        result = _run(agent, _buffered(lambda **kw: directive))
    assert len(observed) == 1
    assert run.shown == safe
    assert result["final_response"] == safe
    assert result["messages"][-1]["content"] == safe
    assert "retired action" not in str(result["messages"])


def test_unchanged_transform_releases_once(agent):
    text = "An ordinary completed answer."
    run = Run(agent, [text])
    result = _run(agent, _buffered(lambda **kw: None))
    assert run.shown == text
    assert result["final_response"] == text


@pytest.mark.parametrize("transformed", [False, True])
def test_release_preserves_context_scrubbing(agent, transformed):
    text = "<memory-context>\nHidden context\n</memory-context>\nVisible answer."
    run = Run(agent, ["Original answer." if transformed else text])
    with patch("agent.turn_finalizer._invoke_hook_safely", side_effect=lambda name, *a, **kw:
               [text] if transformed and name == "transform_llm_output" else []):
        _run(agent, _buffered(lambda **kw: None))
    assert "Hidden context" not in run.shown
    assert "Visible answer." in run.shown


def test_release_preserves_tool_iteration_separator(agent):
    run = Run(agent, ["Final answer."])
    original = run._call

    def call(kwargs):
        agent._stream_needs_break = True
        return original(kwargs)

    agent._interruptible_api_call = call
    agent._interruptible_streaming_api_call = lambda kw, **extra: call(kw)
    _run(agent, _buffered(lambda **kw: None))
    assert run.shown == "\n\nFinal answer."
