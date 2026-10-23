"""Persistent allocation, rollback, and profile isolation contracts."""
import sqlite3

import pytest

from agent.model_routing import ModelRoutingError, resolve_model_route
from tests.agent.test_model_routing import policy


def test_failure_rolls_back_lineage_and_counter(tmp_path):
    path = tmp_path / "routes.db"
    cfg = policy()
    cfg["model_routing"]["routes"]["deep"] = []
    with pytest.raises(ModelRoutingError):
        resolve_model_route("class:deep", config=cfg, session_id="bad", parent_session_id="parent", state_path=path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM aliases").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM counters").fetchone()[0] == 0


def test_profile_state_isolation_and_resume(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    first_home, second_home = tmp_path / "one", tmp_path / "two"
    for home in (first_home, second_home):
        home.mkdir()
        atomic_config_write(home / "config.yaml", policy())
    monkeypatch.setenv("HERMES_HOME", str(first_home))
    first = resolve_model_route("class:deep", session_id="shared-id")
    second = resolve_model_route("class:deep", session_id="another")
    assert second["provider"] != first["provider"]
    monkeypatch.setenv("HERMES_HOME", str(second_home))
    assert resolve_model_route("class:deep", session_id="shared-id") == first
    monkeypatch.setenv("HERMES_HOME", str(first_home))
    assert resolve_model_route("class:deep", session_id="shared-id") == first


def test_revoked_funding_requires_explicit_failure_not_silent_switch(tmp_path):
    cfg = policy()
    args = dict(config=cfg, session_id="same", state_path=tmp_path / "routes.db")
    chosen = resolve_model_route("class:deep", **args)
    for r in cfg["model_routing"]["routes"]["deep"]:
        if r["provider"] == chosen["provider"]:
            r["funding_verified"] = False
    with pytest.raises(ModelRoutingError, match="Sticky route"):
        resolve_model_route("class:deep", **args)
    alternative = resolve_model_route("class:deep", excluded=[(chosen["provider"], chosen["model"])], **args)
    assert alternative["provider"] != chosen["provider"]


def test_lineage_arriving_before_parent_shares_identity(tmp_path):
    args = dict(config=policy(), state_path=tmp_path / "routes.db")
    first = resolve_model_route("class:fast", session_id="child", parent_session_id="parent", **args)
    assert resolve_model_route("class:fast", session_id="parent", **args) == first


def test_config_validation_reports_bad_manifest():
    from hermes_cli.config import validate_config_structure
    cfg = policy()
    cfg["model_routing"]["routes"]["deep"][0]["billing"] = "unlimited-api"
    issues = validate_config_structure(cfg)
    assert any(i.severity == "error" and "billing" in i.message for i in issues)
