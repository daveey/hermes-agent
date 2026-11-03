"""Every resume entry path restores the allocation store's route, not a lagging ``model_config`` copy. [daveey]

MacBook chat 20261026_205117_f67f0d (11-02): an explicit switch moved the allocation store (model-routing.sqlite3)
to gpt-6-astra/openai-codex while the row's ``model_config.model_routing`` still recorded claude-opus-5-5/anthropic.
Resume restored the copy as an exact pin; every agent build (kanban close-out wake, notice wake, automation replay)
then failed with "Exact pinned route cannot fail over", and the chat stayed ``unresolved{notice_tail}``.
"""
import json
from types import SimpleNamespace

import pytest

from agent.model_routing import resolve_model_route
from agent.model_routing_managed import pin_policy
from hermes_cli.config import atomic_config_write
from tests.agent.test_model_routing import policy

SID = "20261026_205117_f67f0d"
OLD = ("anthropic-deep", "anthropic")
NEW = ("openai-codex-deep", "openai-codex")


@pytest.fixture
def switched(tmp_path, monkeypatch):
    """Store pinned on NEW via an honoured explicit switch; returns the session row whose copy still says OLD."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = policy()
    cfg["model_routing"]["enforce_subscription_only"] = True
    atomic_config_write(tmp_path / "config.yaml", cfg)
    for (model, _), replace in ((OLD, False), (NEW, True)):
        level, restricted = pin_policy(model, None, cfg)
        last = resolve_model_route("class:" + level, config=restricted, session_id=SID, exact_pin=True,
                                   replace=replace)
    assert last["metadata"]["selected_model"] == NEW[0]
    stale_copy = {"requested_class": "deep", "policy_version": 1, "selected_provider": OLD[1],
                  "selected_model": OLD[0], "billing_group": "subscription", "reason": "allocation",
                  "exact_pin": True}
    return {"id": SID, "model": NEW[0], "title": "Audit sessions",
            "model_config": json.dumps({"provider": NEW[1], "model": OLD[0], "model_routing": stale_copy}),
            "billing_provider": OLD[1]}


def _db(row):
    return SimpleNamespace(get_session=lambda sid: row if sid == row["id"] else None)


def test_desktop_resume_restores_the_store_route(switched):
    from tui_gateway.server import _stored_session_runtime_overrides
    kept = _stored_session_runtime_overrides(switched)
    assert (kept["model_override"]["model"], kept["provider_override"]) == NEW


def test_cli_resume_restores_the_store_route(switched):
    from tests.hermes_cli.test_resume_model_restore import _make_stub
    stub = _make_stub(model="class:deep", provider="auto", requested_provider="auto", base_url="", api_key="k")
    stub._restore_session_model(switched)
    assert (stub.model, stub.requested_provider) == NEW


def test_gateway_restart_restores_the_store_route(switched):
    from gateway.run_class_routing import stored_route
    runner = SimpleNamespace(session_store=SimpleNamespace(_entries={"k": SimpleNamespace(session_id=SID)}),
                             _session_db=_db(switched))
    assert stored_route(runner, "k") == NEW


def test_api_server_resume_restores_the_store_route(switched):
    from gateway.platforms.api_server_class_routing import restore_session_route
    adapter = SimpleNamespace(_ensure_session_db=lambda: _db(switched))
    assert restore_session_route(adapter, SID, NEW[0], False) == NEW


def test_restored_route_builds_without_refusal(switched):
    """The restored tuple re-pins onto the store's allocation (sticky reuse), the old copy would be refused."""
    from agent.model_routing import ModelRoutingError
    from tui_gateway.server import _stored_session_runtime_overrides
    cfg = policy()
    cfg["model_routing"]["enforce_subscription_only"] = True
    kept = _stored_session_runtime_overrides(switched)
    level, restricted = pin_policy(kept["model_override"]["model"], None, cfg)
    assert resolve_model_route("class:" + level, config=restricted, session_id=SID,
                               exact_pin=True)["metadata"]["selected_model"] == NEW[0]
    level, restricted = pin_policy(OLD[0], None, cfg)
    with pytest.raises(ModelRoutingError, match="Exact pinned route cannot fail over"):
        resolve_model_route("class:" + level, config=restricted, session_id=SID, exact_pin=True)
