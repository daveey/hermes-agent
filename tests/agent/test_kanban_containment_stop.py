"""Containment must be exposed, without treating a failed/foreign stop as terminal."""

from __future__ import annotations

import json

import pytest

from agent.kanban_stop import session_called_kanban_terminal


def test_containment_exposed_in_worker_toolset_and_native_transport():
    from toolsets import TOOLSETS
    from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS

    for name in ("kanban_contain", "kanban_resume_contained"):
        assert name in TOOLSETS["kanban"]["tools"]
        assert name in EXPOSED_TOOLS


@pytest.mark.parametrize("encoded", [False, True])
def test_successful_own_stop_is_terminal(encoded):
    result = {"ok": True, "terminal_for_run": True}
    content = json.dumps(result) if encoded else result
    assert session_called_kanban_terminal(
        [{"role": "tool", "name": "kanban_contain", "content": content}]
    )


@pytest.mark.parametrize(
    "role,name,result",
    [
        ("assistant", "kanban_contain", {"ok": True, "terminal_for_run": True}),
        ("tool", "kanban_resume_contained", {"ok": True, "terminal_for_run": True}),
        ("tool", "other", {"ok": True, "terminal_for_run": True}),
        ("tool", "kanban_contain", {"ok": False, "terminal_for_run": True}),
        ("tool", "kanban_contain", {"ok": 1, "terminal_for_run": True}),
        ("tool", "kanban_contain", {"ok": True, "terminal_for_run": False}),
        ("tool", "kanban_contain", {"ok": True, "terminal_for_run": 1}),
        ("tool", "kanban_contain", {}),
        ("tool", "kanban_contain", None),
        ("tool", "kanban_contain", "malformed"),
        ("tool", "kanban_contain", "[]"),
        ("tool", "kanban_contain", [True]),
    ],
)
def test_nonterminal_result_never_suppresses_nudge(role, name, result):
    message = {"role": role, "name": name, "content": result}
    if role == "assistant":
        message["tool_calls"] = [{"function": {"name": name}}]
    assert not session_called_kanban_terminal([message])
