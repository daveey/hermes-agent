"""A timed-out command in a Desktop terminal batch must not end the turn silently.

Desktop prepares 2+ terminal calls from one assistant message as a batch. When one of them hit the
sequential deadline, the batch guard used to ``agent.interrupt(tool_reason="terminal batch timeout")``:
the turn ended with no assistant reply and the user had to ask "why did you stop" (self-heal
plugin-9605e451d0). ``batch.close()`` already keeps later prepared commands from starting, so the
model must still get the timeout result and its next call, like it does outside a batch. The
preparation timeout had the same turn abort and gets the same contract.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent.terminal_approval_batch as tab
import agent.tool_executor as te
from gateway.session_context import clear_session_vars
from run_agent import AIAgent
from tools import approval


def _agent():
    from tools.file_tools import READ_FILE_SCHEMA
    from tools.terminal_tool import TERMINAL_SCHEMA
    with (
        patch("model_tools.get_tool_definitions", return_value=[
            {"type": "function", "function": schema} for schema in (TERMINAL_SCHEMA, READ_FILE_SCHEMA)
        ]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True, platform="desktop")
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.save_trajectories = False
    return agent


def _call(call_id, command):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(
        name="terminal", arguments=json.dumps({"command": command})))


def _response(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None, reasoning=None)
    finish = "tool_calls" if tool_calls else "stop"
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], model="test/model")
    resp.usage = None
    return resp


def _run_desktop_turn(tmp_path, monkeypatch, calls):
    """One real Desktop turn: the model emits ``calls`` in one message, then replies REPORTED."""
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool_lifecycle import cleanup_vm
    from tui_gateway import server

    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    # Policy approves every command, so the batch runs without a human answer.
    monkeypatch.setattr("tools.approval_context._get_approval_mode", lambda: "off")
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **kw: None)
    monkeypatch.setattr(te, "_resolve_sequential_tool_timeout", lambda: 3.0)

    key = "desktop-batch-timeout"
    agent = _agent()
    approval.register_gateway_notify(key, lambda _data: None)
    monkeypatch.setattr(server, "_sessions", {key: {
        "session_key": key, "source": "desktop", "agent": agent, "cwd": str(tmp_path),
    }})
    tokens = server._set_session_context(key)
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=calls),
        _response(content="REPORTED"),
    ]
    reset_scope = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_CWD": str(tmp_path)})
    try:
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("run the commands", task_id=key)
    finally:
        reset_terminal_scope(reset_scope)
        approval.unregister_gateway_notify(key)
        cleanup_vm(key)
        clear_session_vars(tokens)
        agent.clear_interrupt()
    rows = {m["tool_call_id"]: m["content"] for m in result["messages"] if m.get("role") == "tool"}
    return agent, result, rows


def _assert_turn_replied(agent, result):
    # The model gets its next call, whose text is the turn's reply (not a silent system stop).
    assert result["final_response"] == "REPORTED"
    assert agent.client.chat.completions.create.call_count == 2
    assert not result.get("interrupted")
    exit_reason = str(result.get("turn_exit_reason") or result.get("exit_reason") or "")
    assert "terminal_batch" not in exit_reason


def test_batch_timeout_reports_to_the_model_and_the_turn_replies(tmp_path, monkeypatch):
    third_marker = tmp_path / "third-ran"
    calls = [
        _call("c1", "echo one"),
        _call("c2", "sleep 30; echo two"),
        _call("c3", f"touch '{third_marker}'"),
    ]
    agent, result, rows = _run_desktop_turn(tmp_path, monkeypatch, calls)

    assert set(rows) == {"c1", "c2", "c3"}
    assert "one" in rows["c1"]
    assert "timed out after 3.0s" in rows["c2"]
    _assert_turn_replied(agent, result)
    # The later prepared command still never starts once an earlier one timed out.
    assert not third_marker.exists()
    assert rows["c3"] == tab.COMMAND_TIMEOUT_NOTICE.replace("{name}", "terminal")
    assert "Turn aborted" not in rows["c3"]


def test_stop_closes_the_batch_at_once():
    """``stop`` withdraws the batch immediately (pending approvals, unreleased slots), not only
    when the context manager exits after the sequential loop."""
    batch = tab._TerminalBatch(SimpleNamespace(_interrupt_requested=False), [], "t", [object()])
    batch.stop(tab.COMMAND_TIMEOUT_NOTICE)
    assert batch.stop_notice == tab.COMMAND_TIMEOUT_NOTICE
    assert batch.cancelled.is_set()
    assert batch.slots[0].release.is_set()


def test_preparation_timeout_reports_every_call_and_the_turn_replies(tmp_path, monkeypatch):
    markers = [tmp_path / "first-ran", tmp_path / "second-ran"]
    calls = [_call(f"p{i}", f"touch '{m}'") for i, m in enumerate(markers, 1)]

    def _wedged_start(self):
        raise TimeoutError("Terminal approval preparation timed out; commands were not started")

    monkeypatch.setattr(tab._TerminalBatch, "start", _wedged_start)
    agent, result, rows = _run_desktop_turn(tmp_path, monkeypatch, calls)

    assert not any(m.exists() for m in markers)
    notice = tab.PREPARATION_TIMEOUT_NOTICE.replace("{name}", "terminal")
    assert rows == {"p1": notice, "p2": notice}
    _assert_turn_replied(agent, result)
