"""Host mail identity must not pre-place synthetic admission cards. [daveey]"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_capability_registry as registry
from tests import conftest


@pytest.mark.parametrize("source", ["default", "environment"])
def test_hermetic_environment_hides_host_mail_identity(tmp_path, tmp_path_factory, monkeypatch, source):
    host = tmp_path / "host"
    config = host / ".config" / "hermes-mail" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"gateway": "hub"}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: host))
    if source == "environment":
        monkeypatch.setenv("HERMES_MAIL_CONFIG", str(config))
    else:
        monkeypatch.delenv("HERMES_MAIL_CONFIG", raising=False)
    assert registry.self_gateway() == "hub"  # the actual production reader sees the poison

    sandbox = tmp_path / "nested"
    sandbox.mkdir()
    with pytest.MonkeyPatch.context() as isolated:
        conftest._hermetic_environment.__wrapped__(sandbox, tmp_path_factory, isolated)
        assert registry.self_gateway() is None
