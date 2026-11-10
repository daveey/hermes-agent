"""[daveey] A sandboxed gateway takes launchd's ExitTimeOut from its launcher (gateway-64b702b67e, 2nd fix).

The mini's ``com.daveey.babybear.hermes-household`` / ``-standard`` jobs run
``hermes-gateway-launcher -- /usr/bin/sandbox-exec -p <profile> hermes gateway run``, and the profile denies
``process-exec`` of ``/bin/launchctl`` on purpose (sandbox-escape guard). The gateway's own
``launchctl print`` probe therefore always fails, it assumed the 60 s gui clamp and logged
``launchd_budget_missing`` although launchd kills these jobs at 45 s. The launcher runs unsandboxed: it
probes once and exports ``HERMES_LAUNCHD_EXIT_TIMEOUT_S``; the gateway uses it only when its own probe
fails and its job label is known. A live probe that works still wins.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from gateway.restart import (
    EXTERNAL_GATEWAY_SUPERVISOR_ENV,
    LAUNCHD_EXIT_TIMEOUT_ENV,
    LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S,
    LAUNCHD_LABEL_ENV,
    read_launchd_exit_timeout_s,
)

LABEL = "com.daveey.babybear.hermes-household"


def _raises(cmd, **_kw):
    raise PermissionError(1, "Operation not permitted", "launchctl")  # sandbox-exec deny process-exec


def _nonzero(cmd, **_kw):
    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="sandbox: deny process-exec")


def _live(seconds: int):
    def run(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"\texit timeout = {seconds}\n", stderr="")
    return run


def _env(value: str | None) -> dict[str, str]:
    env = {"XPC_SERVICE_NAME": "0", LAUNCHD_LABEL_ENV: LABEL}
    if value is not None:
        env[LAUNCHD_EXIT_TIMEOUT_ENV] = value
    return env


@pytest.mark.parametrize("run", [_raises, _nonzero], ids=["probe-raises", "probe-nonzero"])
def test_failed_probe_uses_the_launchers_exit_timeout(run):
    assert read_launchd_exit_timeout_s(environ=_env("45"), uid=501, run=run, platform="darwin") == 45.0


@pytest.mark.parametrize("value", ["", "  ", "abc", "0", "-5", "45.5", "4e1", None])
def test_garbage_or_missing_launcher_value_is_ignored(value):
    assert read_launchd_exit_timeout_s(environ=_env(value), uid=501, run=_raises, platform="darwin") is None


def test_live_probe_still_wins_over_the_launcher_value():
    assert read_launchd_exit_timeout_s(environ=_env("45"), uid=501, run=_live(30), platform="darwin") == 30.0


def test_launcher_value_needs_a_known_label():
    env = {"XPC_SERVICE_NAME": "0", LAUNCHD_EXIT_TIMEOUT_ENV: "45"}
    assert read_launchd_exit_timeout_s(environ=env, uid=501, run=_raises, platform="darwin") is None
    assert read_launchd_exit_timeout_s(environ=_env("45"), uid=501, run=_raises, platform="linux") is None


# ── the boot loader: log line and no launchd_budget_missing row ─────────────────────────────────────────


def _loader():
    from gateway.run_config_loaders import GatewayConfigLoadersMixin
    return GatewayConfigLoadersMixin._load_launchd_exit_timeout


def _diag_rows(home: Path) -> list[dict]:
    log = home / "logs" / "gateway-exit-diag.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


@pytest.fixture
def sandboxed_gateway(tmp_path, monkeypatch):
    """A darwin --external-supervisor gateway whose ``launchctl`` cannot run (empty PATH)."""
    home = tmp_path / "home"
    home.mkdir()
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "1")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setenv(LAUNCHD_LABEL_ENV, LABEL)
    monkeypatch.setenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")
    return home


def test_boot_takes_the_launcher_value_and_records_no_missing_budget(sandboxed_gateway, monkeypatch, caplog):
    monkeypatch.setenv(LAUNCHD_EXIT_TIMEOUT_ENV, "45")
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        budget = _loader()(30.0, platform="darwin")
    assert budget == 45.0
    assert f"launchd exit timeout for {LABEL} is 45s" in caplog.text
    assert "(from launcher)" in caplog.text
    assert "launchd_budget_missing" not in caplog.text
    assert _diag_rows(sandboxed_gateway) == []


def test_boot_warning_for_a_too_long_drain_names_the_launcher_source(sandboxed_gateway, monkeypatch, caplog):
    monkeypatch.setenv(LAUNCHD_EXIT_TIMEOUT_ENV, "45")
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        budget = _loader()(180.0, platform="darwin")
    assert budget == 45.0
    assert "live launchd exit timeout (45s)" in caplog.text and "(from launcher)" in caplog.text
    assert _diag_rows(sandboxed_gateway) == []


def test_boot_with_garbage_launcher_value_still_assumes_the_clamp(sandboxed_gateway, monkeypatch, caplog):
    monkeypatch.setenv(LAUNCHD_EXIT_TIMEOUT_ENV, "forty-five")
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        budget = _loader()(30.0, platform="darwin")
    assert budget == float(LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S)
    assert "launchd_budget_missing" in caplog.text
    rows = [r for r in _diag_rows(sandboxed_gateway) if r["tag"] == "gateway.launchd_budget_missing"]
    assert len(rows) == 1 and rows[0]["reason"] == "probe_failed"
