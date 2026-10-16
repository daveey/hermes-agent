"""tool_call / tool_describe accept the provider wire form of a tool name.

On the Anthropic OAuth wire every tool is presented to the model as ``mcp__<name>`` (and a
couple under aliases, e.g. ``session_search`` -> ``chat_history_lookup``). The transport
reverses that for top-level tool_use blocks, but a name the model copies INTO bridge
arguments arrived untranslated, so ``tool_call(name="mcp__kanban_comment")`` failed with
"'mcp__kanban_comment' is not a known tool name … use tool_search" and models looped on it
(coach: three sessions on 2026-10-15, 3-4 identical retries each).
"""

import json

import pytest

from tools.registry import registry
from tools.tool_search import dispatch_tool_describe, resolve_underlying_call
from tools.tool_search_validation import canonical_bridge_tool_name

_PLUGIN = "zz_wire_plugin_tool"
_MCP = "mcp__zzwire__do_thing"


def _schema(name):
    return {"name": name, "description": "x",
            "parameters": {"type": "object", "properties": {"body": {"type": "string"}}}}


@pytest.fixture
def wire_tools():
    registry.register(name=_PLUGIN, toolset="zz-wire-plugin", handler=lambda a, **kw: "{}",
                      schema=_schema(_PLUGIN))
    registry.register(name=_MCP, toolset="mcp-zzwire", handler=lambda a, **kw: "{}",
                      schema=_schema(_MCP))
    yield
    registry.deregister(_PLUGIN)
    registry.deregister(_MCP)


def test_wire_prefixed_deferred_tool_resolves_to_registered_name(wire_tools):
    name, args, err = resolve_underlying_call(
        {"calls": [{"name": f"mcp__{_PLUGIN}", "arguments": {"body": "hi"}}]})
    assert err is None
    assert name == _PLUGIN and args == {"body": "hi"}


def test_wire_prefixed_direct_tool_says_call_directly_not_unknown(wire_tools):
    from tools.registry import discover_builtin_tools
    discover_builtin_tools()
    assert registry.get_entry("read_file") is not None
    # The exact failing shape from the coach sessions: a directly-listed tool, named as the
    # model sees it on the OAuth wire, sent through tool_call.
    name, _args, err = resolve_underlying_call(
        {"calls": [{"name": "mcp__read_file", "arguments": {"path": "x"}}]})
    assert name is None
    assert "not a known tool name" not in err
    assert "'read_file' is a directly-listed tool" in err


def test_registered_mcp_names_and_connectors_are_never_rewritten(wire_tools):
    assert canonical_bridge_tool_name(_MCP) == _MCP
    assert canonical_bridge_tool_name("connectors__gmail__SEND") == "connectors__gmail__SEND"
    assert canonical_bridge_tool_name("mcp__nope_not_registered") == "mcp__nope_not_registered"
    name, _args, err = resolve_underlying_call({"calls": [{"name": _MCP, "arguments": {}}]})
    assert err is None and name == _MCP


def test_unknown_wire_name_still_reports_unknown(wire_tools):
    _name, _args, err = resolve_underlying_call(
        {"calls": [{"name": "mcp__nope_not_registered", "arguments": {}}]})
    assert "not a known tool name" in err


def test_oauth_alias_resolves_to_registered_tool():
    registered = registry.get_entry("session_search") is not None
    if not registered:
        registry.register(name="session_search", toolset="session_search", handler=lambda a, **kw: "{}",
                          schema=_schema("session_search"))
    try:
        assert canonical_bridge_tool_name("chat_history_lookup") == "session_search"
        assert canonical_bridge_tool_name("mcp__chat_history_lookup") == "session_search"
    finally:
        if not registered:
            registry.deregister("session_search")


def test_wire_prefixed_bridge_name_is_still_refused():
    _name, _args, err = resolve_underlying_call({"calls": [{"name": "mcp__tool_call", "arguments": {}}]})
    assert "bridge tool" in err


def test_tool_describe_accepts_wire_name(wire_tools):
    defs = [{"type": "function", "function": _schema(_PLUGIN)}]
    out = json.loads(dispatch_tool_describe({"names": [f"mcp__{_PLUGIN}"]}, current_tool_defs=defs))
    assert _PLUGIN in out["tools"]
    assert "not_found" not in out
