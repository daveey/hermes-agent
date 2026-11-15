"""Nesting exhaustion must produce conservative verdicts, not guard crashes."""
import json

import pytest

from cron import lifecycle_guard as guard
from tools.approval_detection import detect_dangerous_command
from tools.terminal_tool_guards import background_user_chat_block, gateway_lifecycle_block


DEEP = "echo " + "$(echo " * 40 + "true" + ")" * 40


@pytest.mark.parametrize("predicate", [
    guard.contains_gateway_lifecycle_command,
    guard.contains_launchctl_submit_command,
    guard.contains_host_interpreter_kill,
])
def test_predicates_fail_closed_on_nesting_limit(predicate):
    assert predicate(DEEP) is True
    assert predicate("echo harmless") is False


def test_approval_detection_requires_approval_on_nesting_limit():
    dangerous, pattern, description = detect_dangerous_command(DEEP)
    assert dangerous is True
    assert pattern and description


def test_background_guard_refuses_nesting_limit():
    result = json.loads(background_user_chat_block(DEEP, background=True))
    assert "substitution nesting limit" in result["error"]
    assert background_user_chat_block(DEEP, background=False) is None
    assert background_user_chat_block("echo harmless", background=True) is None


def test_gateway_prescan_returns_refusal(monkeypatch, tmp_path):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    result = gateway_lifecycle_block(
        command=DEEP, env=None, env_type="local", cwd=str(tmp_path),
        workdir=None, session_key="nesting-limit",
    )
    assert "Blocked:" in json.loads(result)["error"]


def test_execute_code_refuses_before_backend_acquisition(monkeypatch):
    from tools import code_execution_tool, process_registry, terminal_tool

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(code_execution_tool, "SANDBOX_AVAILABLE", True)

    def unexpected_backend():
        pytest.fail("nesting exhaustion must refuse before acquiring a backend")

    monkeypatch.setattr(terminal_tool, "_get_env_config", unexpected_backend)
    result = json.loads(code_execution_tool.execute_code(DEEP))
    assert "Blocked:" in result["error"]


def test_last_resort_scan_remains_total(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("unavailable scanner")

    monkeypatch.setattr(guard, "_contains_unsafe_gateway_action", broken)
    monkeypatch.setattr(guard, "_direct_lifecycle_scan", broken)
    assert guard.scan_gateway_lifecycle(DEEP)[0] is True
