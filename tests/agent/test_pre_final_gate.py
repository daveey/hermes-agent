"""``pre_final_gate`` hook [daveey]: silent hold / append / limit / exception / scope / buffer."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.pre_final_gate import FLAG
from agent.session_persistence import _is_ephemeral_scaffolding
from run_agent import AIAgent


def _response(content):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")], model="test/model", usage=None,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(
            session_id="pre-final-gate-test", api_key="test-key", base_url="https://example.invalid/v1",
            provider="openai-compat", model="test/model", max_iterations=6, quiet_mode=True,
            skip_context_files=True, skip_memory=True, platform="cli",
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


class Run:
    """Scripted model: each call streams its answer through ``_fire_stream_delta`` first."""

    def __init__(self, agent, answers):
        self.agent, self.answers, self.requests = agent, list(answers), []
        self.screen, self.interims = [], []
        agent.stream_delta_callback = lambda d: self.screen.append(d) if isinstance(d, str) else None
        agent.interim_assistant_callback = lambda text, **_kw: self.interims.append(text)
        agent._interruptible_api_call = self._call
        agent._interruptible_streaming_api_call = lambda kw, on_first_delta=None: self._call(kw)

    def _call(self, api_kwargs):
        self.requests.append(api_kwargs)
        text = self.answers.pop(0)
        self.agent._fire_stream_delta(text)
        return _response(text)

    @property
    def shown(self):
        return "".join(self.screen) + "\n".join(self.interims)


def _run(agent, hook, prompt="hi"):
    with (
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: name == "pre_final_gate"),
        patch("hermes_cli.plugins.iter_hook_callbacks",
              side_effect=lambda name: (hook,) if name == "pre_final_gate" else ()),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        return agent.run_conversation(prompt)


def _buffered(fn):
    fn.needs_buffer = True
    return fn


def test_hold_once_never_shows_or_persists_first_answer(agent):
    run = Run(agent, ["Needs you: please add the token.", "Done: I minted it myself."])
    seen = []

    @_buffered
    def hook(final_response, attempt, turn_messages, **kw):
        seen.append((attempt, final_response, [dict(m) for m in turn_messages]))
        return {"hold": True, "nudge": "Check first; do it yourself."} if attempt == 0 else None

    result = _run(agent, hook)

    assert result["final_response"] == "Done: I minted it myself."
    assert "Needs you" not in run.shown, run.shown
    assert "Done: I minted it myself." in run.shown
    assert [a for a, *_ in seen] == [0, 1]
    # Second call: the model saw its held answer and the private nudge.
    wire = [m.get("content") for m in run.requests[1]["messages"]]
    assert "Needs you: please add the token." in wire and "Check first; do it yourself." in wire
    # Both hold rows are ephemeral (never persisted) and stripped from returned history.
    held_rows = [m for m in seen[1][2] if m.get(FLAG)]
    assert [m["role"] for m in held_rows] == ["assistant", "user"]
    assert all(_is_ephemeral_scaffolding(m) for m in held_rows)
    assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
    assert result["messages"][-1]["content"] == "Done: I minted it myself."


def test_append_delivers_with_note(agent):
    run = Run(agent, ["Here is the answer."])
    result = _run(agent, _buffered(lambda **kw: {"append": "(gate note)"}))
    assert result["final_response"] == "Here is the answer.\n\n(gate note)"
    assert run.shown == "Here is the answer.\n\n(gate note)"
    assert result["messages"][-1]["content"] == "Here is the answer.\n\n(gate note)"


def test_hold_limit_then_delivers_with_append(agent):
    run = Run(agent, ["a1", "a2", "a3"])
    attempts = []

    @_buffered
    def hook(attempt, **kw):
        attempts.append(attempt)
        return {"hold": True, "nudge": "again", "append": "(judge: still asking)"}

    result = _run(agent, hook)
    assert attempts == [0, 1, 2]  # default agent.pre_final_gate_max = 2 holds
    assert len(run.requests) == 3
    assert result["final_response"] == "a3\n\n(judge: still asking)"
    assert "a1" not in run.shown and "a2" not in run.shown


def test_hook_exception_delivers_unchanged_and_logs(agent, caplog):
    run = Run(agent, ["plain answer"])

    def hook(**kw):
        raise RuntimeError("judge down")

    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        result = _run(agent, _buffered(hook))
    assert result["final_response"] == "plain answer"
    assert run.shown == "plain answer"
    assert len(run.requests) == 1
    assert any("pre_final_gate hook" in r.getMessage() and "raised" in r.getMessage() for r in caplog.records)


def test_skipped_for_subagents_unless_opted_in(agent):
    agent._delegate_depth = 1
    calls = []

    def hook(**kw):
        calls.append(1)
        return {"append": "x"}

    Run(agent, ["sub answer"])
    assert _run(agent, hook)["final_response"] == "sub answer" and not calls

    hook.include_background = True
    Run(agent, ["sub answer"])
    assert _run(agent, hook)["final_response"] == "sub answer\n\nx" and calls


def test_no_hook_streams_live(agent):
    run = Run(agent, ["streamed"])
    with patch("hermes_cli.lifecycle.has_hook", return_value=False), \
            patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation("hi")
    assert result["final_response"] == "streamed" and run.shown == "streamed"
    assert agent._final_gate_buffer is None


def test_buffer_released_by_interim_and_discarded_on_hold(agent):
    screen = []
    agent.stream_delta_callback = lambda d: screen.append(d) if isinstance(d, str) else None
    agent.interim_assistant_callback = lambda *_a, **_kw: None
    agent._final_gate_buffer = []
    agent._fire_stream_delta("commentary before tools")
    assert screen == []
    agent._emit_interim_assistant_message({"role": "assistant", "content": "commentary before tools"})
    assert "".join(screen) == "commentary before tools"
    agent._fire_stream_delta("held answer")
    agent._discard_final_gate_buffer()
    agent._flush_final_gate_buffer()
    assert "held answer" not in "".join(screen)


def test_slow_hook_times_out_and_delivers(agent, caplog):
    import threading
    release = threading.Event()
    run = Run(agent, ["fast answer"])

    def hook(**kw):
        release.wait(5)
        return {"hold": True, "nudge": "too late"}

    try:
        with patch("agent.pre_final_gate.timeout_s", return_value=0.2), \
                caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            result = _run(agent, _buffered(hook))
    finally:
        release.set()
    assert result["final_response"] == "fast answer" and run.shown == "fast answer"
    assert any("timed out" in r.getMessage() for r in caplog.records)
