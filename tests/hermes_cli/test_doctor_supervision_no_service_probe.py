"""`hermes doctor`'s gateway-supervision checks never call a service manager.

Regression (release run 40717630046, e2e-upgrade core, ``module-doctor``): in a container with no
gateway unit installed, ``_check_gateway_service_linger`` and ``_check_s6_supervision`` each asked
``detect_service_manager()`` whether s6 was PID 1, and that falls through to
``supports_systemd_services()`` -> ``systemctl --user is-system-running``. Both checks only need
the s6 answer, so they ask ``_s6_running()`` directly.
"""

import shutil

import pytest


@pytest.fixture
def container_without_units(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def record(args, *a, **k):
        calls.append(list(args))
        raise RuntimeError("doctor called systemctl")

    real_which = shutil.which
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr("hermes_cli.gateway.is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.gateway.is_macos", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway.is_windows", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway.is_container", lambda: True)
    monkeypatch.setattr("hermes_cli.gateway.is_wsl", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway.is_termux", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway.shutil.which",
                        lambda name, *a, **k: "/usr/bin/systemctl" if name == "systemctl" else real_which(name, *a, **k))
    monkeypatch.setattr("hermes_cli.gateway._run_systemctl", record)
    monkeypatch.setattr("hermes_cli.service_manager._s6_running", lambda: False)
    return calls


def test_linger_check_does_not_probe_systemd(container_without_units):
    from hermes_cli import doctor_platform

    issues: list[str] = []
    doctor_platform._check_gateway_service_linger(issues)
    assert container_without_units == []
    assert issues == []


def test_s6_check_does_not_probe_systemd(container_without_units):
    from hermes_cli import doctor_platform

    issues: list[str] = []
    doctor_platform._check_s6_supervision(issues)
    assert container_without_units == []
    assert issues == []
