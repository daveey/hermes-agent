"""Exercise real pre-swap bare discovery; no host processes or services are changed."""
from unittest.mock import Mock

import pytest

from hermes_cli import dashboard_procs, gateway, update_cmd_fleet as fleet
from hermes_cli import update_cmd_posix_pause as pause
from hermes_cli import update_fleet_scope as scope, update_pause_deferrals as deferrals

PID = 424242


@pytest.fixture
def bare_probes(tmp_path, monkeypatch):
    import psutil
    from gateway import status
    from hermes_cli import process_identity, update_receipt

    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda pid: ["hermes", "gateway", "run"])
    monkeypatch.setattr(gateway, "_get_service_pids", lambda **kw: {PID})
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda *, exclude_pids, **kw: [PID] if PID not in exclude_pids else [])
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: None)
    monkeypatch.setattr(process_identity, "ledger_entries", lambda **kw: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: Mock(environ=dict, create_time=lambda: 1.0))
    monkeypatch.setattr(pause, "_pid_cgroup", lambda pid="self": "/fixture/session.scope")
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 1.0)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(update_receipt, "_updater_code_root", lambda: tmp_path / "core")
    monkeypatch.setattr(update_receipt, "_gateway_code_root", lambda pid, home: tmp_path / "core")
    monkeypatch.setattr(scope, "update_scope_homes", lambda: {tmp_path.resolve()})
    monkeypatch.setattr(deferrals, "state_path", lambda: tmp_path / "streak.json")
    monkeypatch.setattr(deferrals, "report", Mock())
    # The sole mutation seam: discovery and scope classification remain real.
    stop = Mock()
    monkeypatch.setattr(pause, "_stop_gateways", stop)
    return stop


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("hidden_service", [False, True])
def test_unknown_bare_home_refuses_with_attributable_streak(bare_probes, monkeypatch, hidden_service):
    monkeypatch.setattr(gateway, "_get_service_pids", lambda **kw: {PID} if hidden_service else set())
    for attempt in range(3):
        verdict = pause._pause(pause._empty_token())
        assert verdict and "checkout was not changed" in verdict
        assert f"gateway PID {PID}" in verdict and "home" in verdict
        assert deferrals._read()[f"gateway PID {PID}"]["count"] == attempt + 1
    bare_probes.assert_not_called()
    assert isinstance(deferrals.report, Mock)
    deferrals.report.assert_called_once()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("reason", ["foreign", "pinned"])
def test_proven_bare_exclusion_succeeds(bare_probes, tmp_path, monkeypatch, capsys, reason):
    from hermes_cli import update_receipt
    home = tmp_path / "foreign" if reason == "foreign" else tmp_path
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(home))
    monkeypatch.setattr(update_receipt, "_gateway_code_root", lambda pid, home: tmp_path / "pin")
    assert pause._pause(pause._empty_token()) is None
    assert "left alone" in capsys.readouterr().out
    bare_probes.assert_not_called()


@pytest.mark.platforms("linux")
def test_normal_own_home_bare_is_not_hidden_by_best_effort_service_pid(bare_probes, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(tmp_path))
    token = pause._empty_token()
    assert pause._pause(token) is None
    bare_probes.assert_called_once()
    assert token["unmapped_pids"] == [PID]
    assert token["unmapped"][0]["home"] == str(tmp_path)


@pytest.mark.platforms("linux")
def test_unclaimed_supervised_gateway_refuses_instead_of_becoming_bare(bare_probes, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(tmp_path))
    monkeypatch.setattr(gateway, "_capture_gateway_argv", lambda pid: ["hermes", "gateway", "run", "--external-supervisor"])
    verdict = pause._pause(pause._empty_token())
    assert verdict and "--external-supervisor" in verdict and "checkout was not changed" in verdict
    bare_probes.assert_not_called()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("pinned", [False, True])
def test_unit_gateway_never_enters_bare_stop_set(bare_probes, tmp_path, monkeypatch, pinned):
    import subprocess
    from hermes_cli import update_receipt

    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "_ensure_user_systemd_env", lambda: None)
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(tmp_path))
    monkeypatch.setattr(update_receipt, "_gateway_code_root", lambda pid, home: tmp_path / ("pin" if pinned else "core"))

    def systemctl(cmd, **kwargs):
        if "list-units" in cmd:
            value = "hermes-gateway-fixture.service loaded active running gateway" if "--user" in cmd else ""
        elif "--property=MainPID" in cmd:
            value = str(PID)
        elif "--property=ControlGroup" in cmd:
            value = "/fixture/gateway.service"
        else:
            pytest.fail(f"unexpected systemctl call: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, value, "")

    monkeypatch.setattr(fleet, "_systemctl", systemctl)
    token = pause._empty_token()
    assert pause._pause(token) is None
    assert token["unmapped"] == []
    if pinned:
        bare_probes.assert_not_called()
        assert token["posix_units"] == []
    else:
        bare_probes.assert_called_once()
        assert [entry["pid"] for entry in token["posix_units"]] == [PID]


@pytest.mark.platforms("linux")
def test_default_bare_discovery_keeps_best_effort_exclusion(bare_probes, monkeypatch):
    monkeypatch.delenv(pause.STOP_BUSY_ENV)
    assert pause._pause(pause._empty_token()) is None
    bare_probes.assert_not_called()

