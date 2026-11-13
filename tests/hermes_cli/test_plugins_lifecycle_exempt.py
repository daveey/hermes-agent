"""A skipped (slow/hung) fail-closed pre_tool_call guard must not refuse a worker's Kanban park/report calls.

2026-11-12, David-MBP: one ``_pre_tool_call`` callback timed out; for the next 60 s core refused every tool
call, ``kanban_continue`` / ``kanban_comment`` / ``kanban_heartbeat`` included, with a message naming neither
the plugin nor when to retry. The worker could not park and the run was booked a protocol violation.
"""

from __future__ import annotations

import logging
import re
import threading

import pytest

from hermes_cli.plugins import _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE, PluginManager

MODULE = "hermes_plugins.slow_guard"
LIFECYCLE = ("kanban_block", "kanban_continue", "kanban_schedule", "kanban_heartbeat", "kanban_comment",
             "kanban_request_review")


@pytest.fixture
def slow_guard(monkeypatch):
    """A manager whose one pre_tool_call guard (module ``MODULE``) hangs past a 0.1 s timeout."""
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.1)
    hold = threading.Event()

    def _pre_tool_call(**_kwargs):
        hold.wait(timeout=10.0)

    _pre_tool_call.__module__ = MODULE
    mgr = PluginManager()
    mgr._hooks["pre_tool_call"] = [_pre_tool_call]
    yield mgr
    hold.set()


def _call(mgr, tool, n):
    return mgr.invoke_hook("pre_tool_call", tool_name=tool, args={}, tool_call_id=f"call-{tool}-{n}")


def _blocked(results):
    return [r for r in results if isinstance(r, dict) and r.get("action") == "block"]


def test_timeout_refusal_names_plugin_and_retry(slow_guard, caplog):
    """The call that times out and every later call in the window are refused with the plugin module and
    the seconds left; the timed-out WARNING names the module too."""
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        first = _blocked(_call(slow_guard, "terminal", 0))
        later = _blocked(_call(slow_guard, "terminal", 1))
    for results in (first, later):
        assert len(results) == 1
        msg = results[0]["message"]
        assert msg.startswith(_PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE)
        assert MODULE in msg and "_pre_tool_call" in msg
        seconds = int(re.search(r"retry in (\d+) s", msg).group(1))
        assert 50 <= seconds <= 60
    timed_out = [r.getMessage() for r in caplog.records if "timed out after" in r.getMessage()]
    assert len(timed_out) == 1 and MODULE in timed_out[0]
    skipped = [r.getMessage() for r in caplog.records if "skipped after previous timeout" in r.getMessage()]
    assert len(skipped) == 1 and MODULE in skipped[0] and "retry in" in skipped[0]


@pytest.mark.parametrize("tool", LIFECYCLE)
def test_lifecycle_tools_run_during_suppression(slow_guard, caplog, tool):
    """Inside the 60 s window after the timeout, each lifecycle tool is not refused; one WARNING names the
    skipped callback. A ``terminal`` call in the same window is still refused."""
    assert _blocked(_call(slow_guard, "terminal", 0))  # the guard times out: window opens
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        assert _blocked(_call(slow_guard, tool, 1)) == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert MODULE in warnings[0] and "_pre_tool_call" in warnings[0] and tool in warnings[0]
    refused = _blocked(_call(slow_guard, "terminal", 2))
    assert len(refused) == 1 and MODULE in refused[0]["message"] and "retry in" in refused[0]["message"]


def test_kanban_complete_stays_fail_closed(slow_guard):
    """Control: completing the card is not a park/report call; a skipped guard still refuses it."""
    assert _blocked(_call(slow_guard, "terminal", 0))
    refused = _blocked(_call(slow_guard, "kanban_complete", 1))
    assert len(refused) == 1 and MODULE in refused[0]["message"]


def test_lifecycle_call_that_times_out_itself_runs(slow_guard):
    """The exemption also covers the call on which the guard times out."""
    assert _blocked(_call(slow_guard, "kanban_continue", 0)) == []


def test_lifecycle_tool_reaches_its_handler_e2e(slow_guard, monkeypatch):
    """E2E through ``handle_function_call``: during the suppression window ``kanban_comment`` is
    dispatched while ``terminal`` is not."""
    import json
    from unittest.mock import MagicMock, patch

    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_plugin_manager", slow_guard)
    dispatched = []

    def _dispatch(name, args, **_kwargs):
        dispatched.append(name)
        return json.dumps({"ok": True})

    registry = MagicMock()
    registry.dispatch.side_effect = _dispatch
    with patch("model_tools.registry", registry):
        from model_tools import handle_function_call
        refused = handle_function_call("terminal", {"command": "true"}, task_id="t1", session_id="s1")
        ran = handle_function_call("kanban_comment", {"body": "x"}, task_id="t1", session_id="s1")
    assert MODULE in refused and "retry in" in refused
    assert dispatched == ["kanban_comment"]
    assert json.loads(ran) == {"ok": True}
