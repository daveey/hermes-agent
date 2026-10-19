"""post_tool_call fires before transform_tool_result on every executor path.

``model_tools._apply_transform_tool_result_hook`` documents "Runs after post_tool_call".
A plugin whose ``post_tool_call`` queues a hint and whose ``transform_tool_result``
attaches it relies on that order; when the sequential registry path ran the transform
inside ``handle_function_call`` (post suppressed there, emitted later by the executor),
the hint landed on the NEXT tool result instead.
"""

import pytest

from agent import inline_tool_executors
from tests.agent import test_run_agent as _base
from tests.agent.test_run_agent import _mock_assistant_msg, _mock_tool_call

agent = _base.agent
_mock_plugin_discovery = _base._mock_plugin_discovery


def _run(agent, monkeypatch, *, concurrent: bool, tool: str):
    """Drive one executor with a hint-queueing plugin; returns (hook order, messages)."""
    events: list[tuple[str, str]] = []
    queued: dict[str, str] = {}

    def _hook(hook_name, **kw):
        call_id = kw.get("tool_call_id")
        if hook_name == "post_tool_call":
            events.append(("post", call_id))
            queued[call_id] = f"HINT-{call_id}"
            return []
        if hook_name == "transform_tool_result":
            events.append(("transform", call_id))
            hint = queued.pop(call_id, None)
            return [f"{kw['result']}\n{hint}"] if hint else []
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _hook)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _hook, raising=False)
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True, raising=False)

    if tool == "inline":
        name = "todo_list"
        monkeypatch.setitem(inline_tool_executors.INLINE_TOOL_EXECUTORS, name,
                            lambda _agent, _args, _ctx: '{"output": "inline"}')
        agent.valid_tool_names = set(agent.valid_tool_names or ()) | {name}
    else:
        name = "web_search"
        from tools.registry import registry
        monkeypatch.setattr(registry, "dispatch", lambda n, a, **kw: '{"output": "registry"}')

    ids = ("c1", "c2") if concurrent else ("c1",)
    calls = [_mock_tool_call(name=name, arguments='{"q": "x"}', call_id=i) for i in ids]
    msg = _mock_assistant_msg(content="", tool_calls=calls)
    messages: list = []
    agent.session_id = "session-1"
    run = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
    run(msg, messages, "task-1")
    return events, messages, ids


@pytest.mark.parametrize("tool", ["registry", "inline"])
@pytest.mark.parametrize("concurrent", [False, True], ids=["sequential", "concurrent"])
def test_post_tool_call_fires_before_transform_tool_result(agent, monkeypatch, concurrent, tool):
    events, messages, ids = _run(agent, monkeypatch, concurrent=concurrent, tool=tool)

    for call_id in ids:
        mine = [kind for kind, cid in events if cid == call_id]
        assert mine == ["post", "transform"], (call_id, events)

    # The hint queued by THIS call's post_tool_call lands on THIS call's result.
    results = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    for call_id in ids:
        content = results[call_id]
        text = content if isinstance(content, str) else str(content)
        assert f"HINT-{call_id}" in text, (call_id, text)


@pytest.mark.parametrize("via_thread", [True, False], ids=["rpc-thread", "same-thread"])
def test_nested_call_transforms_its_own_result(agent, monkeypatch, via_thread):
    """A tool call made from inside a sequential registry tool (execute_code's sandbox RPC thread
    inherits the executor's context via propagate_context_to_thread) runs its OWN
    transform_tool_result inline on the inner result; only the outer hook touches the outer result."""
    import threading

    import model_tools
    from tools.registry import registry
    from tools.thread_context import propagate_context_to_thread

    events: list[tuple[str, str, str]] = []

    def _hook(hook_name, **kw):
        if hook_name in ("post_tool_call", "transform_tool_result"):
            events.append((hook_name, kw.get("tool_name"), kw.get("tool_call_id") or ""))
        if hook_name == "transform_tool_result":
            return [f"{kw['result']}|T({kw.get('tool_name')})"]
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", _hook)
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _hook, raising=False)
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: True, raising=False)

    inner: dict = {}

    def _dispatch(name, args, **kw):
        if name == "web_search":
            return '{"output": "inner"}'

        def _rpc():
            inner["r"] = model_tools.handle_function_call("web_search", {"query": "x"}, task_id="task-1")

        if via_thread:
            t = threading.Thread(target=propagate_context_to_thread(_rpc), daemon=True)
            t.start()
            t.join(10)
        else:
            _rpc()
        return '{"output": "outer"}'

    monkeypatch.setattr(registry, "dispatch", _dispatch)

    msg = _mock_assistant_msg(content="", tool_calls=[
        _mock_tool_call(name="web_extract", arguments='{"urls": ["https://x"]}', call_id="c1")])
    messages: list = []
    agent.session_id = "session-1"
    agent._execute_tool_calls_sequential(msg, messages, "task-1")

    assert inner["r"] == '{"output": "inner"}|T(web_search)', inner
    outer = [m["content"] for m in messages if m.get("role") == "tool"]
    assert len(outer) == 1
    text = outer[0] if isinstance(outer[0], str) else str(outer[0])
    assert "|T(web_extract)" in text and "T(web_search)" not in text, text
    # The outer call still runs post_tool_call before its transform.
    mine = [h for h, name, cid in events if cid == "c1"]
    assert mine == ["post_tool_call", "transform_tool_result"], events
