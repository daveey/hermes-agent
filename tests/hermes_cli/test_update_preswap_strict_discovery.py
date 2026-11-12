"""Opted-in discovery must prove exclusions, not mistake failed probes for emptiness."""
from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from hermes_cli import gateway, update_cmd_fleet as fleet
from hermes_cli import update_cmd_posix_pause as pause
from hermes_cli import update_fleet_scope as scope, update_pause_deferrals as deferrals

UNIT = "hermes-gateway-review"
REAL_STOP_GATEWAYS = pause._stop_gateways


@pytest.fixture
def probes(tmp_path, monkeypatch):
    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "_ensure_user_systemd_env", lambda: None)
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: ([], []))
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(scope, "gateway_pid_home", lambda pid: None)
    from hermes_cli import dashboard_procs
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: None)
    monkeypatch.setattr(deferrals, "state_path", lambda: tmp_path / "streak.json")
    report = Mock(return_value="fixture-repair")
    monkeypatch.setattr(deferrals, "report", report)
    stop = Mock()
    monkeypatch.setattr(pause, "_stop_gateways", stop)
    return stop, report


@pytest.mark.parametrize("failure,identity", [
    ("listing", "systemd user gateway inventory"),
    ("missing", "systemd user gateway inventory"),
    ("pid_nonzero", UNIT), ("pid_invalid", UNIT), ("pid_empty", UNIT),
    ("ownership", UNIT), ("implicit_default", UNIT), ("cgroup_timeout", UNIT),
])
def test_unknown_systemd_discovery_refuses_and_third_attempt_reports(probes, monkeypatch, failure, identity):
    stop, report = probes

    def systemctl(cmd, **kwargs):
        if "list-units" in cmd:
            if "--user" not in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if failure == "missing":
                raise FileNotFoundError("no systemctl")
            return subprocess.CompletedProcess(cmd, int(failure == "listing"),
                                               f"{UNIT}.service loaded active running gateway\n", "bus failed")
        if "--property=MainPID" in cmd:
            return subprocess.CompletedProcess(cmd, int(failure == "pid_nonzero"),
                                               "invalid" if failure == "pid_invalid" else "" if failure == "pid_empty" else "424242", "")
        if "--property=Environment" in cmd:
            if failure == "cgroup_timeout":
                return subprocess.CompletedProcess(cmd, 0, "HERMES_HOME=/fixture/home", "")
            return subprocess.CompletedProcess(cmd, 0 if failure == "implicit_default" else 1, "", "denied")
        if "--property=ControlGroup" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        pytest.fail(f"unexpected probe: {cmd}")

    monkeypatch.setattr(scope, "home_in_update_scope", lambda home: failure != "implicit_default")
    monkeypatch.setattr(scope, "gateway_foreign_code_root", lambda pid, home: None)
    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    for attempt in range(3):
        verdict = pause._pause(pause._empty_token())
        assert verdict and "checkout was not changed" in verdict
        assert deferrals._read()[identity]["count"] == attempt + 1
    stop.assert_not_called()
    report.assert_called_once()
    assert report.call_args.args[0] == identity


def test_default_discovery_keeps_best_effort_semantics(probes, monkeypatch):
    monkeypatch.delenv(pause.STOP_BUSY_ENV)
    monkeypatch.setattr(fleet, "_systemctl", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "bus failed"))
    assert pause._pause(pause._empty_token()) is None


@pytest.mark.parametrize("reason", ["foreign", "pinned", "inactive"])
def test_proven_exclusions_do_not_refuse(probes, monkeypatch, reason):
    def systemctl(cmd, **kwargs):
        if "list-units" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"{UNIT}.service loaded active running gateway\n" if "--user" in cmd else "", "")
        if "--property=MainPID" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "0" if reason == "inactive" else "424242", "")
        if "--property=ActiveState" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "inactive", "")
        if "--property=Environment" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "HERMES_HOME=/fixture/home", "")
        pytest.fail(f"unexpected probe: {cmd}")
    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    monkeypatch.setattr(scope, "home_in_update_scope", lambda home: reason != "foreign")
    monkeypatch.setattr(scope, "gateway_foreign_code_root", lambda pid, home: "/fixture/pin" if reason == "pinned" else None)
    assert pause._pause(pause._empty_token()) is None


def test_expired_stop_names_all_remaining_entries():
    entries = [{"pid": 42, "unit": UNIT}, {"pid": 43}]
    with pytest.raises(pause.PauseRefused) as error:
        pause._stop_busy_gateways({}, entries, deadline=0)
    assert error.value.units == [UNIT, "gateway PID 43"]


@pytest.mark.parametrize("mode", ["privilege", "deadline", "nonzero", "exception"])
def test_async_stop_refusal_names_its_unit(monkeypatch, mode):
    monkeypatch.setattr(pause, "_manage_cmd", lambda s: None if mode == "privilege" else ["systemctl"])
    def systemctl(*args, **kwargs):
        if mode == "exception":
            raise OSError("denied")
        return subprocess.CompletedProcess([], 1, "", "denied")
    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    with pytest.raises(pause.PauseRefused) as error:
        pause._systemctl_async({"scope": "user", "unit": UNIT}, ["stop"], 0 if mode == "deadline" else float("inf"), must=True)
    assert error.value.units == [UNIT]


def test_stop_verification_failures_report_third_attempt_and_rollback(probes, monkeypatch):
    from hermes_cli import update_cmd_windows as windows, update_pause_record as record
    from gateway import status

    _, report = probes
    # Use the real stop transaction and async systemctl helper, never a host mutation.
    monkeypatch.setattr(pause, "_stop_gateways", REAL_STOP_GATEWAYS)
    entries = [{"kind": "systemd", "scope": "user", "unit": UNIT, "pid": 424242},
               {"pid": 424243, "argv": ["fixture"]}]
    monkeypatch.setattr(pause, "_discover_systemd", lambda: entries[:1])
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: (entries[1:], []))
    monkeypatch.setattr(pause, "_left_running", lambda u, j: (u, j, []))
    clock = [100.0]
    class Clock:
        @staticmethod
        def monotonic():
            return clock[0]
        @staticmethod
        def sleep(seconds):
            clock[0] += seconds
    monkeypatch.setattr(pause, "time", Clock)
    monkeypatch.setattr(pause, "_PRE_SWAP_DRAIN_WAIT_S", 1.0)
    monkeypatch.setattr(pause, "_manage_cmd", lambda s: ["systemctl"])
    monkeypatch.setattr(pause, "_ask_busy_to_drain", lambda *a: False)
    monkeypatch.setattr(pause, "_alive", lambda pid, ct: pid == 424242)
    monkeypatch.setattr(pause.os, "kill", lambda *a: None)
    monkeypatch.setattr(fleet, "_systemctl", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 1)
    monkeypatch.setattr(record, "identity", lambda pid=None: {"ct": "ct:1"})
    for name in ("record_pause", "mark_stop_requested", "mark_stop_sent", "sync"):
        monkeypatch.setattr(record, name, lambda *a, **k: None)
    resumed = Mock()
    monkeypatch.setattr(windows, "_resume_windows_gateways_after_update", resumed)
    for attempt in range(3):
        verdict = pause._pause(pause._empty_token())
        assert verdict and "did not stop" in verdict
        assert deferrals._read()[UNIT]["count"] == attempt + 1
        assert "gateway PID 424243" not in deferrals._read()  # already verified stopped
    assert resumed.call_count == 3
    report.assert_called_once()
    assert report.call_args.args[0] == UNIT
