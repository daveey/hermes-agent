"""Upgrade fixtures must enter real class admission with only loopback credentials."""
import json

import pytest
import hermes_yaml as yaml

from hermes_cli.kanban_model_tier import class_unroutable_reason
from tests.e2e.core.upgrade import _install_helpers as I


@pytest.mark.parametrize("level", ["fast", "standard", "deep"])
def test_upgrade_fixture_supplies_real_class_route(tmp_path, monkeypatch, level):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("model:\n  default: fake-model\n")
    I.configure_kanban_routing(home, "http://127.0.0.1:12345/v1")
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["model"]["default"] == "fake-model"
    assert class_unroutable_reason(f"class:{level}", cfg) is None
    cfg["model_routing"]["enabled"] = False
    assert class_unroutable_reason(f"class:{level}", cfg) == "routing_disabled"
    pool = json.loads((home / "auth.json").read_text())["credential_pool"]["custom"]
    assert pool[0]["base_url"] == "http://127.0.0.1:12345/v1"
    assert pool[0]["access_token"] == "loopback-only"


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://127.0.0.2/v1"])
def test_upgrade_fixture_rejects_nonfixture_endpoints(tmp_path, url):
    with pytest.raises(AssertionError):
        I.configure_kanban_routing(tmp_path, url)
    assert not (tmp_path / "auth.json").exists()
