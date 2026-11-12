"""``tool_call`` entries whose tool name is missing or misplaced.

Shapes are the real ones from the coach's "tool_call calls[<n>] requires a 'name'" sessions
(2026-11-10/11): the envelope nested under ``arguments``, the name written beside the tool's own
params, and a later batch entry that dropped its name expecting to reuse the first entry's tool.
"""

import json

import pytest

from tools.registry import registry
from tools.tool_search import resolve_underlying_call
from tools.tool_search_validation import normalize_tool_call_entries

_TOOLS = {
    # action_request-shaped: no `name` parameter.
    "fake_action_request": {"receipt": {"type": "object"}, "request": {"type": "object"}},
    # tailnet_exec-shaped.
    "fake_tailnet_exec": {"command": {"type": "string"}, "host": {"type": "string"}},
    # A tool whose own parameter is called `name` (skill/profile style).
    "fake_named_param": {"name": {"type": "string"}, "content": {"type": "string"}},
    # A registered tool whose schema declares no properties: nothing proves `name` is not its arg.
    "fake_no_props": None,
}


@pytest.fixture(autouse=True)
def _fake_tools():
    for tool, props in _TOOLS.items():
        params = {"type": "object"} if props is None else {"type": "object", "properties": props}
        registry.register(
            name=tool, toolset="fake-entry-name", handler=lambda a, **kw: "{}",
            schema={"name": tool, "description": "x", "parameters": params})
    yield
    for tool in _TOOLS:
        registry.deregister(tool)


def test_nested_envelope_is_unwrapped():
    # 20261110_171433_c8a00a: the legacy single shape put inside one entry's arguments.
    inner = {"command": "echo hi", "reason": "r", "wait_s": 900}
    entries, err = normalize_tool_call_entries(
        {"calls": [{"arguments": {"name": "fake_tailnet_exec", "arguments": inner}}]})
    assert err is None
    assert entries == [{"name": "fake_tailnet_exec", "arguments": inner}]


def test_nested_envelope_with_string_arguments_and_no_arguments():
    entries, err = normalize_tool_call_entries(
        {"calls": [{"arguments": json.dumps({"name": "fake_tailnet_exec",
                                             "arguments": json.dumps({"host": "mini"})})}]})
    assert err is None and entries == [{"name": "fake_tailnet_exec", "arguments": {"host": "mini"}}]
    entries, err = normalize_tool_call_entries({"calls": [{"arguments": {"name": "fake_tailnet_exec"}}]})
    assert err is None and entries == [{"name": "fake_tailnet_exec", "arguments": {}}]


def test_nested_connector_envelope_is_unwrapped():
    entries, err = normalize_tool_call_entries(
        {"calls": [{"arguments": {"name": "connectors__gmail__SEND_EMAIL", "arguments": {"to": "x"}}}]})
    assert err is None
    assert entries == [{"name": "connectors__gmail__SEND_EMAIL", "arguments": {"to": "x"}}]


def test_name_beside_params_is_lifted_and_dispatches():
    # 20261111_091916_d2d36a: {"arguments": {"request": ..., "name": "action_request"}}.
    request = {"schemaVersion": 2, "kind": "approval"}
    name, args, err = resolve_underlying_call(
        {"calls": [{"arguments": {"request": request, "name": "fake_action_request"}}]})
    assert err is None
    assert (name, args) == ("fake_action_request", {"request": request})


def test_name_beside_params_is_kept_when_the_tool_declares_name():
    # `name` is that tool's own argument: lifting it would invent the tool choice.
    entries, err = normalize_tool_call_entries(
        {"calls": [{"arguments": {"name": "fake_named_param", "content": "x"}}]})
    assert entries == []
    assert "calls[0] requires a 'name'" in err


@pytest.mark.parametrize("args", [
    # A nameless call to another tool Y, e.g. skill_view(name=X, file_path=...), whose own
    # `name` argument happens to equal a registered tool X: rerouting it to X would run a
    # tool the model never chose. Every other key must be one of X's declared params.
    {"name": "fake_action_request", "file_path": "references/api.md"},
    {"name": "fake_action_request", "request": {}, "file_path": "references/api.md"},
])
def test_name_beside_params_foreign_to_that_tool_is_not_rerouted(args):
    entries, err = normalize_tool_call_entries({"calls": [{"arguments": args}]})
    assert entries == []
    assert "calls[0] requires a 'name'" in err
    _, _, err = resolve_underlying_call({"calls": [{"arguments": args}]})
    assert err and "calls[0] requires a 'name'" in err


@pytest.mark.parametrize("args", [
    {"name": "not_a_registered_tool", "content": "x"},  # beside params, unknown tool
    {"name": "fake_no_props", "content": "x"},  # beside params, schema declares nothing
    {"name": "not_a_registered_tool", "arguments": {}},  # nested envelope, unknown tool
    {"name": "connectors__gmail__SEND_EMAIL", "to": "x"},  # beside params, no local schema
    {"name": "   ", "arguments": {}},
    {"name": 7, "arguments": {}},
    "not json {",
    ["fake_tailnet_exec"],
])
def test_unrecoverable_shapes_stay_errors(args):
    entries, err = normalize_tool_call_entries({"calls": [{"arguments": args}]})
    assert entries == []
    assert "calls[0] requires a 'name'" in err


def test_nested_bridge_name_is_still_refused():
    entries, err = normalize_tool_call_entries(
        {"calls": [{"arguments": {"name": "tool_search", "arguments": {}}}]})
    assert entries == [] and "itself a bridge tool" in err


def test_dropped_name_in_a_local_batch_names_both_corrections():
    # 20261110_223014_e339f4: calls[1] repeated calls[0]'s params without a name. It is never
    # inherited; the error says so, names the sibling and the no-local-batch rule in one turn.
    args = {"command": "git log -1", "host": "mini", "reason": "r", "timeout": 60}
    entries, err = normalize_tool_call_entries({"calls": [
        {"name": "fake_tailnet_exec", "arguments": dict(args, host="mbp")},
        {"arguments": args},
    ]})
    assert entries == []
    assert err.startswith("tool_call calls[1] requires a 'name'")
    assert "it carries command, host, reason, timeout" in err
    assert "never inherited" in err and "'fake_tailnet_exec'" in err
    assert "send each local tool call as its own tool_call" in err
    assert err.endswith("Nothing in this batch was invoked.")


def test_dropped_name_in_a_connector_batch_omits_the_local_batch_rule():
    _, err = normalize_tool_call_entries({"calls": [
        {"name": "connectors__gmail__SEND_EMAIL", "arguments": {}}, {"arguments": {"to": "x"}}]})
    assert "'connectors__gmail__SEND_EMAIL'" in err
    assert "own tool_call" not in err


def test_single_nameless_entry_reports_its_keys_only():
    # 20261111_184938_f73858: one entry, arguments {command}, no name anywhere.
    _, err = normalize_tool_call_entries({"calls": [{"arguments": {"command": "cat q3.js"}}]})
    assert "calls[0] requires a 'name'" in err and "it carries command" in err
    assert "this batch also names" not in err and "own tool_call" not in err
    _, err = normalize_tool_call_entries({"calls": [{"query": "x"}]})
    assert "it carries query" in err
    _, err = normalize_tool_call_entries({"calls": [{"arguments": {f"k{i}": i for i in range(10)}}]})
    assert "k0, k1, k2, k3, k4, k5, k6, k7, ...)" in err
    _, err = normalize_tool_call_entries({"calls": [{}]})
    assert "carries" not in err
