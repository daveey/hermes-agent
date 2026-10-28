"""agent.tool_executor imports into a process that cached the previous release's agent.interrupt_control.

During ``hermes update`` a long-lived process (gateway, dashboard) keeps its old modules in
``sys.modules`` while the files on disk become HEAD. The dashboard then lazily imported HEAD's
tool_executor, which needed ``interrupt_skip_wording`` (added after v2026.9.24), and got
ImportError (e2e-upgrade handoff, release run 40717630046, dashboard.log).
"""
from __future__ import annotations

import importlib
import sys
import types


def test_tool_executor_imports_against_previous_release_interrupt_control(monkeypatch):
    real = importlib.import_module("agent.interrupt_control")
    old = types.ModuleType(real.__name__)
    for k, v in vars(real).items():
        if k != "interrupt_skip_wording" and not k.startswith("__"):
            setattr(old, k, v)
    monkeypatch.setitem(sys.modules, "agent.interrupt_control", old)
    monkeypatch.delitem(sys.modules, "agent.tool_executor", raising=False)
    m = importlib.import_module("agent.tool_executor")
    assert m.interrupt_skip_wording(types.SimpleNamespace(_tool_interrupt_reason=None)) == "Turn interrupted"
    assert m.interrupt_skip_wording(types.SimpleNamespace(_tool_interrupt_reason="user interrupt")) == "User interrupt"
