"""Offline harness children must not start detached default-plugin installers."""

from pathlib import Path

import pytest

from hermes_cli.default_plugins import fetch_policy
from tests.e2e.core.chaos._helpers import hermetic_env
from tests.e2e.core.parity._helpers import ParityHome
from tests.e2e.core.providers._oauth_helpers import make_home


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("harness", ["chaos", "parity", "oauth"])
def test_child_environment_denies_lazy_installs(tmp_path, monkeypatch, harness):
    # Even an ambient opt-in must not let a hermetic fixture fetch plugins.
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "0")
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    if harness == "chaos":
        env = hermetic_env(home, hermes_home, "isolation")
    elif harness == "parity":
        fixture = ParityHome(tmp_path, home, hermes_home, tmp_path, tmp_path,
                             tmp_path / "pids", "isolation")
        env = fixture.env()
    else:
        env = make_home(tmp_path).env()
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", env.get("HERMES_DISABLE_LAZY_INSTALLS", ""))
    assert fetch_policy(Path(env["HERMES_HOME"])) == "denied"
