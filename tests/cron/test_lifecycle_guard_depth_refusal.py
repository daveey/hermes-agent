"""Depth exhaustion is a scan failure, not a detected lifecycle command."""

import json
from types import SimpleNamespace

import pytest

from cron.lifecycle_guard import (
    GatewayLifecycleBlocked,
    check_gateway_lifecycle,
    scan_gateway_lifecycle,
)
from tools.terminal_tool_guards import gateway_lifecycle_block


def _chain(tmp_path, length, final="echo harmless"):
    for index in range(length):
        content = f"sh {tmp_path / f'step{index + 1}.sh'}" if index + 1 < length else final
        (tmp_path / f"step{index}.sh").write_text(content + "\n", encoding="utf-8")
    return f"sh {tmp_path / 'step0.sh'}"


def _terminal_message(command, tmp_path, monkeypatch):
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    result = gateway_lifecycle_block(
        command=command, env=SimpleNamespace(cwd=str(tmp_path)), env_type="local",
        cwd=str(tmp_path), workdir=str(tmp_path), session_key="depth-refusal-test",
    )
    assert result is not None
    return json.loads(result)["error"]


def test_deep_harmless_chain_names_scan_refusal(tmp_path):
    unsafe, refusal = scan_gateway_lifecycle(_chain(tmp_path, 12), cwd=str(tmp_path))
    assert unsafe is True
    assert refusal is not None
    assert "scan depth limit was reached (depth 8)" in refusal


def test_deep_harmless_chain_terminal_names_scan_refusal(tmp_path, monkeypatch):
    message = _terminal_message(_chain(tmp_path, 12), tmp_path, monkeypatch)
    assert "could not scan" in message
    assert "scan depth limit was reached" in message
    assert "cannot restart, stop, or uninstall the gateway" not in message


def test_deep_harmless_chain_cron_names_scan_refusal(tmp_path):
    with pytest.raises(GatewayLifecycleBlocked) as excinfo:
        check_gateway_lifecycle(_chain(tmp_path, 12), None)
    assert "could not scan" in str(excinfo.value)
    assert "scan depth limit was reached" in str(excinfo.value)


def test_inert_depth_cap_does_not_taint_later_true_refusal(tmp_path):
    from cron import lifecycle_guard

    budget = lifecycle_guard._LifecycleScanBudget()
    assert lifecycle_guard._contains_unsafe_gateway_action(
        "echo harmless", cwd=str(tmp_path),
        depth=lifecycle_guard._MAX_REFERENCED_SCRIPT_DEPTH,
        visited=set(), budget=budget, executed=False,
    ) is False
    assert budget.refusal is None
    assert lifecycle_guard._contains_unsafe_gateway_action(
        "hermes gateway restart", cwd=str(tmp_path), depth=0,
        visited=set(), budget=budget,
    ) is True
    assert budget.refusal is None


def test_short_lifecycle_chain_retains_true_refusal(tmp_path, monkeypatch):
    command = _chain(tmp_path, 3, "hermes gateway restart")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    message = _terminal_message(command, tmp_path, monkeypatch)
    assert "cannot restart, stop, or uninstall the gateway" in message
    assert "could not scan" not in message
