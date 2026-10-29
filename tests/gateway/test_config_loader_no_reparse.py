"""GET /api/status loads the gateway config every request; an unchanged config.yaml must not re-parse."""
import hermes_yaml
import utils
from gateway import config_loader


def test_read_yaml_layers_parses_unchanged_config_once(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_yaml, "_cache_dir", lambda: tmp_path / "cache")
    (tmp_path / "config.yaml").write_text("# pad\n" * 4000 + "gateway:\n  platforms: {}\nx: yes\n", encoding="utf-8")
    parses = []
    real = hermes_yaml._load
    monkeypatch.setattr(hermes_yaml, "_load", lambda *a, **k: parses.append(1) or real(*a, **k))
    utils._YAML_FILE_CACHE.clear()

    first = config_loader.read_yaml_layers(tmp_path)
    first["x"] = "caller mutation"  # the shared cached object must not leak caller edits
    second = config_loader.read_yaml_layers(tmp_path)
    assert len(parses) == 1
    assert second["x"] is True

    (tmp_path / "config.yaml").write_text("# pad\n" * 4000 + "x: no\n", encoding="utf-8")
    assert config_loader.read_yaml_layers(tmp_path)["x"] is False
    assert len(parses) == 2
