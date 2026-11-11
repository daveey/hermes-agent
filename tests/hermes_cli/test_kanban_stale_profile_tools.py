"""Stale remote observations are unknown, not affirmative tool absence."""
import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_runs_on_capabilities as rc


@pytest.mark.parametrize("checked", [0, True, None, "bad", float("nan"), float("inf"), 10001])
def test_stale_or_invalid_tools_allow_move_but_leave_claim_gate(monkeypatch, checked):
    monkeypatch.setattr(kp.reg, "remote_gateway_profiles", lambda _: ["default"])
    monkeypatch.setattr(kp.reg, "stale_after", lambda: 1800)
    record = {"worker": {"schema": 1, "capabilities": [
        {"name": "profile-tools:default:terminal", "ok": True, "checked_at": checked}]}}
    info = kp.target_info("worker", now=10000, remote=record)
    assert info["tools"] is None
    got = rc.check(["machine:worker"], assignee="default", requires=["terminal", "file", "git"],
                   me="hub", exclude_me=True, machines={"worker": {"state": "active"}},
                   remote_tools=lambda _: info["tools"])
    assert got == {"worker": None}


def test_fresh_negative_still_refuses_missing_tools(monkeypatch):
    monkeypatch.setattr(kp.reg, "remote_gateway_profiles", lambda _: ["default"])
    monkeypatch.setattr(kp.reg, "stale_after", lambda: 1800)
    record = {"worker": {"schema": 1, "capabilities": [
        {"name": "profile-tools:default:terminal", "ok": False, "checked_at": 9999}]}}
    info = kp.target_info("worker", now=10000, remote=record)
    assert info["tools"] == {}
    with pytest.raises(rc.PlacementCapabilityMismatch):
        rc.check(["machine:worker"], assignee="default", requires=["terminal"], me="hub",
                 machines={"worker": {"state": "active"}}, remote_tools=lambda _: info["tools"])
