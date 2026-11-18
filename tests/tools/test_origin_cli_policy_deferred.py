"""Compose the native policy with the actual deferred dispatcher and registry."""
import json

import pytest


@pytest.fixture
def dispatch(monkeypatch):
    import model_tools
    import tools.tool_search as ts
    from tools.origin_cli_policy import current_policy
    from tools.registry import registry

    calls = []
    original = registry._tools.copy()
    schema = {"name": "kanban_create", "description": "fixture", "parameters": {
        "type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}}
    registry.register(name="kanban_create", toolset="kanban", schema=schema,
                      handler=lambda args, **kw: calls.append(args) or json.dumps({"ok": True}))
    registry.register(name="origin_policy_probe", toolset="terminal",
                      schema={"name": "origin_policy_probe", "parameters": {"type": "object", "properties": {}}},
                      handler=lambda args, **kw: json.dumps(current_policy().create(args)))
    cfg = ts.ToolSearchConfig.from_raw({"defer": ["kanban_create"]})
    monkeypatch.setattr(ts, "load_config_readonly", lambda: cfg)
    # Keep real bridge validation, hook dispatch and registry execution. Catalog is
    # supplied explicitly so this test never probes the host's kanban availability.
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [{"type": "function", "function": schema}])
    monkeypatch.setattr(model_tools, "_select_tool_names", lambda *a, **kw: {"kanban_create"})
    def run(enabled=("terminal", "tool_call"), args=None, **kw):
        return json.loads(model_tools.handle_function_call(
            "origin_policy_probe", {"title": "child"} if args is None else args,
            enabled_tools=list(enabled), session_id="parent-session", turn_id="parent-turn", **kw))
    yield run, calls
    registry._tools.clear()
    registry._tools.update(original)


def test_delegated_context_cannot_use_parent_policy(dispatch):
    from agent.delegation_context import delegated_child_context
    run, calls = dispatch
    with delegated_child_context():
        assert not run().get("ok")
    assert calls == []


def test_create_owns_post_hook_under_executor_suppression(dispatch, monkeypatch):
    import model_tools
    from hermes_cli import lifecycle
    run, calls = dispatch
    seen = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "post_tool_call")
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kw: seen.append((name, kw)) or [])
    with model_tools.suppress_post_tool_call_hook():
        assert run()["ok"]
    events = [kw for name, kw in seen if name == "post_tool_call" and kw["tool_name"] == "kanban_create"]
    assert len(events) == 1
    assert events[0]["session_id"] == "parent-session"
    assert calls == [{"title": "child"}]


@pytest.mark.parametrize("allowed", [True, False])
def test_real_rpc_default_dispatch_preserves_parent_policy(dispatch, allowed):
    from contextvars import copy_context
    from concurrent.futures import ThreadPoolExecutor
    import time
    import model_tools
    from tools import code_execution_rpc as rpc
    from tools.registry import registry
    _run, calls = dispatch
    def outer(args, **kw):
        ctx = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(ctx.run, rpc._handle_rpc_request,
                {"tool": "origin_policy_probe", "args": args},
                allowed_tools=frozenset({"origin_policy_probe"}), tool_call_counter=[0],
                max_tool_calls=1, dispatch=rpc._default_dispatch("inner-task"),
                tool_call_log=[], call_start=time.monotonic(), where="fixture").result(timeout=10)
    registry.register(name="origin_rpc_probe", toolset="terminal",
                      schema={"name": "origin_rpc_probe", "parameters": {}}, handler=outer)
    result = json.loads(model_tools.handle_function_call(
        "origin_rpc_probe", {"title": "child"}, session_id="parent-session",
        enabled_tools=["tool_call"] if allowed else ["terminal"]))
    assert bool(result.get("ok")) is allowed
    assert calls == ([{"title": "child"}] if allowed else [])


def test_configured_deferred_create_runs(dispatch):
    run, calls = dispatch
    assert run()["ok"]
    assert calls == [{"title": "child"}]


@pytest.mark.parametrize("enabled", [("terminal",), (), ("terminal", "tool_describe")])
def test_deferred_requires_visible_call_permission(dispatch, enabled):
    run, calls = dispatch
    assert not run(enabled).get("ok")
    assert calls == []


def test_deferred_scope_removed_after_capture(dispatch, monkeypatch):
    import model_tools
    run, calls = dispatch
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [])
    assert "not available" in run()["error"]
    assert calls == []


def test_deferred_schema_validated(dispatch):
    run, calls = dispatch
    assert "missing required" in run(args={})["error"]
    assert calls == []


def test_deferred_respects_current_eager_policy(dispatch, monkeypatch):
    import tools.tool_search as ts
    run, calls = dispatch
    monkeypatch.setattr(ts, "load_config_readonly", lambda: ts.ToolSearchConfig.from_raw({"defer": [], "eager": ["kanban_create"]}))
    assert "directly-listed" in run()["error"]
    assert calls == []
