"""[daveey] The launchd gateway at the END of the real job chain must know its job label (gateway-cc28b19bca).

launchd stamps ``XPC_SERVICE_NAME=<label>`` only on the job's main process. Since the signed-bundle wrapper
and the core-update launcher went in front of ``stderr_timestamp``, every process below the wrapper reads
``XPC_SERVICE_NAME=0`` (measured on the MacBook), so ``stderr_timestamp``'s re-export found nothing, the
gateway's ``launchd_job_label()`` was ``None``, the stop drain was never capped to launchd's 60 s
``ExitTimeOut`` and launchd restarted into a still-draining gateway (start_refused owner_alive=true).
The plist now names the label in ``EnvironmentVariables``, which every descendant inherits; a darwin
``--external-supervisor`` gateway that still finds no label assumes the gui-domain clamp and says so.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_cli.gateway as gateway_cli
from gateway.restart import (
    EXTERNAL_GATEWAY_SUPERVISOR_ENV,
    LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S,
    LAUNCHD_LABEL_ENV,
    LAUNCHD_STOP_CLEANUP_RESERVE_S,
    resolve_launchd_capped_drain,
)
from hermes_cli.gateway_launchd import _SIGNED_WRAPPER_SOURCE, launchd_program_arguments

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process chain")

# The gateway stand-in at the leaf: records the label the real reader resolves from its environment.
_LEAF_PROBE = (
    "import sys; from gateway.restart import launchd_job_label; "
    "open(sys.argv[1], 'w').write(str(launchd_job_label()))"
)


def _plist(tmp_path, monkeypatch, label: str = "ai.hermes.gateway") -> dict:
    profile_dir = tmp_path / ".hermes"
    profile_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
    monkeypatch.setattr(gateway_cli, "get_python_path", lambda: sys.executable)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: label)
    return plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))


@pytest.mark.parametrize("label", ["ai.hermes.gateway", "ai.hermes.gateway-butler"])
def test_plist_environment_names_the_job_label(tmp_path, monkeypatch, label):
    env = _plist(tmp_path, monkeypatch, label)["EnvironmentVariables"]
    assert env[LAUNCHD_LABEL_ENV] == label


def test_gateway_at_end_of_real_launchd_chain_resolves_its_label(tmp_path, monkeypatch):
    """Run the generated job: the plist's EnvironmentVariables, launchd's XPC_SERVICE_NAME on the FIRST
    process only (the signed wrapper), ``XPC_SERVICE_NAME=0`` from the next process down (the launcher's
    place in the chain, as macOS stamps it), then the real ``stderr_timestamp`` and a leaf gateway stand-in."""
    plist = _plist(tmp_path, monkeypatch)
    out = tmp_path / "leaf-label.txt"
    leaf = [sys.executable, "-c", _LEAF_PROBE, str(out)]
    chain = ["/usr/bin/env", "XPC_SERVICE_NAME=0", sys.executable, "-m", "hermes_cli.stderr_timestamp",
             "--error-log", str(tmp_path / "err.log"), "--", *leaf]
    program = launchd_program_arguments(chain, tmp_path / "out.log", tmp_path / "err.log",
                                        responsible=Path(sys.executable))
    assert program[3] == _SIGNED_WRAPPER_SOURCE  # the real signed wrapper is the job's main process
    job_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO_ROOT),
        **plist["EnvironmentVariables"],
        "XPC_SERVICE_NAME": plist["Label"],
    }
    proc = subprocess.run(program, env=job_env, cwd=str(REPO_ROOT), timeout=60, check=False)
    assert proc.returncode == 0, (tmp_path / "err.log").read_text() if (tmp_path / "err.log").exists() else ""
    assert out.read_text() == "ai.hermes.gateway"


def _loader():
    from gateway.run_config_loaders import GatewayConfigLoadersMixin
    return GatewayConfigLoadersMixin._load_launchd_exit_timeout


def _diag_rows(home: Path) -> list[dict]:
    log = home / "logs" / "gateway-exit-diag.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


@pytest.fixture
def darwin_unlabelled(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.delenv(LAUNCHD_LABEL_ENV, raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "1")
    return home


def test_external_supervisor_without_label_assumes_launchd_clamp_and_records_it(darwin_unlabelled, monkeypatch, caplog):
    monkeypatch.setenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        budget = _loader()(180.0, platform="darwin")
    assert budget == float(LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S)
    assert resolve_launchd_capped_drain(180.0, budget) == 60.0 - LAUNCHD_STOP_CLEANUP_RESERVE_S
    rows = [r for r in _diag_rows(darwin_unlabelled) if r["tag"] == "gateway.launchd_budget_missing"]
    assert len(rows) == 1 and rows[0]["level"] == "warning"
    assert rows[0]["assumed_exit_timeout_s"] == 60
    assert "launchd_budget_missing" in caplog.text


def test_foreground_darwin_gateway_without_label_keeps_no_budget(darwin_unlabelled, monkeypatch):
    """A gateway started from a shell (no --external-supervisor) is not launchd-timed: nothing assumed."""
    monkeypatch.delenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    assert _loader()(180.0, platform="darwin") is None
    assert _diag_rows(darwin_unlabelled) == []


def test_linux_external_supervisor_without_label_keeps_no_budget(darwin_unlabelled, monkeypatch):
    monkeypatch.setenv(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")
    assert _loader()(180.0, platform="linux") is None
    assert _diag_rows(darwin_unlabelled) == []
