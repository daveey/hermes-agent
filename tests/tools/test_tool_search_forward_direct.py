"""[daveey] tool_call forwarding for directly-listed tools (tools.tool_search.forward_direct_calls).

Coach signature: "'process_manage' is a directly-listed tool, not a deferred one. Call it
directly instead of via tool_call. The tool was NOT invoked." A session called
process_manage through tool_call successfully while it was in ``tool_search.defer``; the
shared config then moved it to ``eager`` mid-session, and every later tool_call to it — the
shape the model's own history showed working — was refused.
"""
import json
from unittest.mock import patch

import pytest

import tools.tool_search as ts
from tools.registry import registry

_NAME = "fwd_direct_probe_tool"


def _td(name):
    return {"type": "function", "function": {"name": name, "description": "x",
                                             "parameters": {"type": "object", "properties": {
                                                 "action": {"type": "string"}},
                                                 "required": ["action"]}}}


@pytest.fixture
def probe_tool():
    registry.register(name=_NAME, toolset="fwd-direct-plugin",
                      handler=lambda a, **kw: json.dumps({"ran": a.get("action")}),
                      schema=_td(_NAME)["function"])
    yield _NAME
    registry.deregister(_NAME)


def _cfg(monkeypatch, **raw):
    monkeypatch.setattr(ts, "load_config_readonly", lambda: ts.ToolSearchConfig.from_raw(raw))


_CALL = {"calls": [{"name": _NAME, "arguments": {"action": "wait"}}]}


def test_default_is_upstream_refusal_after_mid_session_eager_flip(probe_tool, monkeypatch):
    _cfg(monkeypatch)  # deferred (plugin tool): the bridge works
    assert ts.resolve_underlying_call(_CALL) == (_NAME, {"action": "wait"}, None)
    _cfg(monkeypatch, eager=[_NAME])  # config flips mid-session
    name, _a, err = ts.resolve_underlying_call(_CALL)
    assert name is None and "directly-listed tool" in err
    assert ts.scoped_deferrable_names([_td(_NAME)]) == frozenset()


def test_forward_runs_directly_listed_tool_through_bridge(probe_tool, monkeypatch):
    _cfg(monkeypatch, eager=[_NAME], forward_direct_calls=True)
    assert ts.resolve_underlying_call(_CALL) == (_NAME, {"action": "wait"}, None)
    assert _NAME in ts.scoped_deferrable_names([_td(_NAME)])
    from model_tools import handle_function_call
    with patch("model_tools.get_tool_definitions", return_value=[_td(_NAME)]):
        out = json.loads(handle_function_call("tool_call", _CALL, task_id="t"))
    assert out == {"ran": "wait"}


def test_forward_unwraps_in_agent_loop_executor(probe_tool, monkeypatch):
    # The agent loop peels tool_call in agent.tool_executor (own scope gate), not model_tools.
    from types import SimpleNamespace
    from agent.tool_executor import _unwrap_tool_search_call
    _cfg(monkeypatch, eager=[_NAME], forward_direct_calls=True)
    with patch("model_tools.get_tool_definitions", return_value=[_td(_NAME)]):
        agent = SimpleNamespace(enabled_toolsets=None, disabled_toolsets=None)
        assert _unwrap_tool_search_call(agent, "tool_call", _CALL) == (_NAME, {"action": "wait"}, None)
    with patch("model_tools.get_tool_definitions", return_value=[]):
        agent = SimpleNamespace(enabled_toolsets=None, disabled_toolsets=None)
        name, _a, block = _unwrap_tool_search_call(agent, "tool_call", _CALL)
    assert name == "tool_call" and "not available in this session" in block


def test_forward_still_enforces_session_scope_and_schema(probe_tool, monkeypatch):
    _cfg(monkeypatch, eager=[_NAME], forward_direct_calls=True)
    from model_tools import handle_function_call
    with patch("model_tools.get_tool_definitions", return_value=[]):  # not in this session
        out = json.loads(handle_function_call("tool_call", _CALL, task_id="t"))
    assert "not available in this session" in out["error"]
    with patch("model_tools.get_tool_definitions", return_value=[_td(_NAME)]):
        out = json.loads(handle_function_call(
            "tool_call", {"calls": [{"name": _NAME, "arguments": {}}]}, task_id="t"))
    assert "missing required argument" in out["error"]


def test_forward_never_admits_bridge_or_unknown_names(monkeypatch):
    _cfg(monkeypatch, forward_direct_calls=True)
    _n, _a, err = ts.resolve_underlying_call({"calls": [{"name": "nope_not_registered", "arguments": {}}]})
    assert "not a known tool name" in err
    _n, _a, err = ts.resolve_underlying_call({"calls": [{"name": "tool_search", "arguments": {}}]})
    assert "bridge tool" in err
    assert ts.scoped_deferrable_names([_td("tool_call"), _td("tool_search")]) == frozenset()


def test_forward_config_parses_and_defaults_off():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["tools"]["tool_search"]["forward_direct_calls"] is False
    assert ts.ToolSearchConfig.from_raw(None).forward_direct_calls is False
    assert ts.ToolSearchConfig.from_raw({"forward_direct_calls": "true"}).forward_direct_calls is True
    assert ts.ToolSearchConfig.from_raw({"forward_direct_calls": "garbage"}).forward_direct_calls is False
