"""Background plugin checks must not recreate renamed/deleted profile homes."""

import shutil
from contextlib import contextmanager

import pytest

from hermes_cli import plugins_cadence as cadence
from hermes_cli.plugins_updates import CheckResult
from hermes_constants import set_hermes_home_override, reset_hermes_home_override, mark_named_profile_deleted
from pm import receipt


@contextmanager
def hermes_home_override(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("when", ["before", "during", "live"])
@pytest.mark.parametrize("retirement", ["rename", "delete", "tombstone"])
def test_gateway_check_respects_profile_retirement(tmp_path, monkeypatch, when, retirement):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = tmp_path / "profiles" / "gamma"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  auto_apply: true\n")
    calls, applied = [], []

    def retire():
        if retirement == "rename":
            home.rename(home.with_name("delta"))
        elif retirement == "delete":
            shutil.rmtree(home)
        else:
            mark_named_profile_deleted(home)

    def checks(directory):
        calls.append(directory)
        if when == "during":
            retire()
        return [CheckResult("plug", "git", update_available=True)]

    monkeypatch.setattr(cadence, "_default_config_get", lambda s, k: True if k == "auto_apply" else 24)
    with hermes_home_override(home):
        if when == "before":
            retire()
        cadence.maybe_run_gateway_check(run_checks_fn=checks, apply_updates_fn=applied.append)
        if when == "live":
            saved = receipt.latest()
            assert saved is not None and saved["outcome"] == "updates-available"
            assert (home / "plugin-update-checks" / "last-run").is_file()
            assert (home / "plugin-update-checks" / "results.json").is_file()
        else:
            assert calls == ([] if when == "before" else [home / "plugins"])
            assert not (home / "plugin-update-checks").exists()
            assert not (home / "logs").exists()
            assert home.exists() == (retirement == "tombstone")
        assert applied == (["plug"] if when == "live" else [])
