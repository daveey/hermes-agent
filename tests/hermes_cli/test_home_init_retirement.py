"""A profile retired during config initialization must not be resurrected."""
import shutil

import pytest

from hermes_cli import config, config_home
from hermes_constants import mark_named_profile_deleted


@pytest.mark.parametrize("operation", ["rename", "delete"])
@pytest.mark.parametrize("boundary", ["root", "child"])
def test_initializer_refuses_retirement_after_outer_live_check(
    tmp_path, monkeypatch, operation, boundary,
):
    root = tmp_path / "state"
    home = root / "profiles" / "gamma"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(config, "get_hermes_home", lambda: home)
    monkeypatch.setattr(config, "_HERMES_HOME_ENSURED", set())
    monkeypatch.setattr(config, "is_managed", lambda: False)
    original = config_home._ensure_directory
    retired = []
    target = home if boundary == "root" else home / "cron"

    def retire_before_directory(path, **kwargs):
        if path == target and not retired:
            retired.append(path)
            mark_named_profile_deleted(home)
            if operation == "rename":
                home.rename(home.with_name("delta"))
            else:
                shutil.rmtree(home)
        return original(path, **kwargs)

    monkeypatch.setattr(config_home, "_ensure_directory", retire_before_directory)
    with pytest.raises(config_home.HomeInitializationError):
        config.ensure_hermes_home()
    assert retired == [target]
    assert not home.exists()
    assert str(home) not in config._HERMES_HOME_ENSURED
    if operation == "rename":
        assert (home.with_name("delta") / "config.yaml").read_text() == "{}"


@pytest.mark.parametrize("named", [False, True])
def test_initializer_preserves_fresh_default_and_live_named_home(tmp_path, monkeypatch, named):
    root = tmp_path / "state"
    home = root / "profiles" / "gamma" if named else root
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(config, "is_managed", lambda: False)
    if named:
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}")
    ensured = set()
    config_home.initialize_home(home, ("cron", "logs/curator"), ensured)
    assert (home / "cron").is_dir()
    assert (home / "logs/curator").is_dir()
    assert (home / "SOUL.md").is_file()
    assert str(home) in ensured
