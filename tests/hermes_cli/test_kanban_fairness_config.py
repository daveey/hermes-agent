"""Disabled-by-default fairness evidence configuration contract."""
import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.kanban_fairness_config import resolve


def test_default_disabled_creates_nothing(tmp_path):
    defaults = DEFAULT_CONFIG["kanban"]["fairness_capture"]
    assert defaults == {"enabled": False, "capture_id": None, "directory": None,
                        "max_bytes": 536870912}
    assert resolve({}, home=tmp_path) is None
    assert resolve({"kanban": {"fairness_capture": defaults}}, home=tmp_path) is None
    assert not (tmp_path / "kanban").exists()
    assert not (tmp_path / "custom").exists()


def test_explicit_capture_uses_profile_durable_path(tmp_path):
    config = {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "run-1"}}}
    result = resolve(config, home=tmp_path)
    assert result.capture_id == "run-1"
    assert result.directory == tmp_path / "kanban" / "evidence" / "run-1"
    assert result.max_bytes == 536870912
    config["kanban"]["fairness_capture"].update(directory=str(tmp_path / "custom"), max_bytes=4096)
    assert resolve(config, home=tmp_path).directory == tmp_path / "custom"
    assert resolve(config, home=tmp_path).max_bytes == 4096
    assert not (tmp_path / "kanban").exists()
    assert not (tmp_path / "custom").exists()


@pytest.mark.parametrize("options", [
    True, [], {"enabled": "false"}, {"enabled": True},
    {"enabled": True, "capture_id": ".."},
    {"enabled": True, "capture_id": "../escape"},
    {"enabled": True, "capture_id": "x", "directory": "relative"},
    {"enabled": True, "capture_id": "x", "max_bytes": True},
    {"enabled": True, "capture_id": "x", "max_bytes": 0},
    {"enabled": True, "capture_id": "x", "max_bytes": 536870913},
    {"enabled": True, "capture_id": "x", "typo": 1},
])
def test_invalid_capture_is_rejected_without_io(tmp_path, options):
    with pytest.raises(ValueError):
        resolve({"kanban": {"fairness_capture": options}}, home=tmp_path)
    assert not (tmp_path / "kanban").exists()
    assert not (tmp_path / "custom").exists()
