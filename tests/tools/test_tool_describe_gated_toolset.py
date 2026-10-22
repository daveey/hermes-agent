"""[daveey] tool_describe on a registered tool whose toolset is off for this session.

Failure (2026-10-21, mini): ~/AGENTS.md requires kanban_create; the mini's cli platform had kanban (default-off)
disabled, so the tool was absent from the session. tool_describe answered "'kanban_create' is a directly-listed
tool ... Call it directly" — but it was in no list, so the agent gave up. The error must say it is not enabled
and how to enable it.
"""
import json

import pytest

import tools.tool_search as ts
from tools.registry import registry

_NAME = "gated_probe_tool"


def _td(name):
    return {"type": "function", "function": {"name": name, "description": "x",
                                             "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture
def probe_tool(monkeypatch):
    registry.register(name=_NAME, toolset="probe-ts", handler=lambda a, **kw: "{}", schema=_td(_NAME)["function"])
    monkeypatch.setattr(ts, "load_config_readonly", lambda: ts.ToolSearchConfig.from_raw({"eager": [_NAME]}))
    yield _NAME
    registry.deregister(_NAME)


def test_describe_absent_from_session_names_the_toolset_and_fix(probe_tool):
    out = json.loads(ts.dispatch_tool_describe({"names": [_NAME]}, current_tool_defs=[]))
    err = out["errors"][_NAME]
    assert "NOT enabled in this session" in err
    assert "hermes tools enable probe-ts --platform cli" in err
    assert "Call it directly" not in err


def test_describe_present_in_session_still_says_call_directly(probe_tool):
    out = json.loads(ts.dispatch_tool_describe({"names": [_NAME]}, current_tool_defs=[_td(_NAME)]))
    assert "directly-listed tool" in out["errors"][_NAME]
