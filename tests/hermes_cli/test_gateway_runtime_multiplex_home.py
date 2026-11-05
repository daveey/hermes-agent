"""Only exact named-profile homes may route to a multiplexer."""
from pathlib import Path

import pytest

from hermes_cli import gateway_runtime_multiplex as multiplex
from hermes_constants import named_profile_home


@pytest.mark.parametrize("root_name", [".hermes", "custom-root"])
def test_exact_profile_home_and_nested_custom_home(tmp_path, monkeypatch, root_name):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / root_name
    profile = root / "profiles" / "test"
    profile.mkdir(parents=True)
    if root_name == "custom-root":
        (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    nested = profile / "cache" / "scratch" / "pytest" / "home"
    nested.mkdir(parents=True)

    assert named_profile_home(nested) == profile
    assert multiplex.multiplexer_root_for(profile) == root
    assert multiplex.multiplexer_root_for(nested) is None
    assert multiplex.implied_host_root(nested) is None
    assert multiplex.multiplexer_serves_home(nested) is None


def test_ordinary_home_has_no_multiplexer(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "ordinary-home"
    home.mkdir()
    assert multiplex.multiplexer_root_for(home) is None


def test_nested_home_discovery_does_not_probe_ancestor(tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime, gateway_runtime_discovery

    home = tmp_path / ".hermes" / "profiles" / "test" / "cache" / "scratch" / "home"
    home.mkdir(parents=True)

    def reject_foreign_probe(*args, **kwargs):
        pytest.fail("nested custom home must not probe a multiplexer socket")

    monkeypatch.setattr(gateway_runtime_discovery, "query_identify", reject_foreign_probe)
    assert gateway_runtime._served_by_multiplexer(home, timeout=1.0) is None
