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


@pytest.mark.parametrize("reject", [False, True])
def test_prepared_failover_is_invisible_until_commit_and_rolls_back(tmp_path, reject):
    import json

    path = tmp_path / "routes.db"
    args = dict(config=policy(), session_id="parent", state_path=path)
    first = resolve_model_route("class:deep", **args)
    from agent.model_routing_state import RoutingState
    RoutingState(path).link_continuation("child", "parent")
    with sqlite3.connect(path) as db:
        counters = db.execute("SELECT * FROM counters ORDER BY pool").fetchall()

    def prepare(metadata):
        assert metadata["selected_provider"] != first["provider"]
        # Another connection (including a restarted reader) cannot see a
        # replacement whose credential/client preparation hasn't succeeded.
        with sqlite3.connect(path) as reader:
            stored = json.loads(reader.execute("SELECT metadata FROM routes").fetchone()[0])
            assert stored == first["metadata"]
        if reject:
            raise RuntimeError("client failed")

    failure = [(first["provider"], first["model"])]
    if reject:
        with pytest.raises(RuntimeError, match="client failed"):
            resolve_model_route("class:deep", excluded=failure, prepare=prepare, **args)
        expected = first
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT * FROM counters ORDER BY pool").fetchall() == counters
    else:
        expected = resolve_model_route("class:deep", excluded=failure, prepare=prepare, **args)
    assert resolve_model_route("class:deep", **args) == expected
    args["session_id"] = "child"
    assert resolve_model_route("class:deep", **args) == expected


def test_config_validation_reports_bad_manifest():
    from hermes_cli.config import validate_config_structure
    cfg = policy()
    cfg["model_routing"]["routes"]["deep"][0]["billing"] = "unlimited-api"
    issues = validate_config_structure(cfg)
    assert any(i.severity == "error" and "billing" in i.message for i in issues)
