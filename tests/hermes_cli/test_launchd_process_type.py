"""[daveey] The launchd gateway plist runs the job as ProcessType=Interactive.

Measured on the MacBook: with no ProcessType every thread of the gateway (the Desktop's local backend) ran
at scheduler priority 20, and under load its event loop stalled past the Desktop's 4 s request timeout.
Interactive jobs run at 31. An installed plist without the key must read as stale so the next
install/update rewrites it.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import hermes_cli.gateway as gateway_cli
from hermes_cli import gateway_launchd
from hermes_cli import gateway_service_owner


def _home(tmp_path, monkeypatch) -> Path:
    profile_dir = tmp_path / ".hermes"
    profile_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
    monkeypatch.setattr(gateway_cli, "get_python_path", lambda: sys.executable)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    return profile_dir


def test_generated_plist_runs_gateway_as_interactive(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    plist = plistlib.loads(gateway_cli.generate_launchd_plist().encode("utf-8"))
    assert plist["ProcessType"] == "Interactive"


def test_installed_plist_without_process_type_is_stale_and_rewritten(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    plist_path = tmp_path / "ai.hermes.gateway.plist"
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_service_owner, "refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gateway_service_owner, "definition_belongs_to_home", lambda *a: True)
    monkeypatch.setattr(gateway_cli, "_prepare_service_launcher", lambda: None)

    current = gateway_cli.generate_launchd_plist()
    old = plistlib.loads(current.encode("utf-8"))
    del old["ProcessType"]
    plist_path.write_bytes(plistlib.dumps(old))
    assert gateway_cli.launchd_plist_is_current() is False

    # The stale-plist rewrite (install/update path for an unloaded or reloading job) writes the key back.
    assert gateway_launchd._rewrite_stale_launchd_plist() is True
    assert plistlib.loads(plist_path.read_bytes())["ProcessType"] == "Interactive"
    assert gateway_cli.launchd_plist_is_current() is True
