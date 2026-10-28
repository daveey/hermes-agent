"""Resume adapters preserve pin permission even after allocation state is lost."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.model_routing import ModelRoutingError
from agent.model_routing_restore import restore_route
from agent.model_routing_state import RoutingState
from hermes_cli.config import atomic_config_write
from hermes_cli.runtime_provider import resolve_runtime_provider
from hermes_state import SessionDB
from tests.agent import test_model_routing_agent as fixtures

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


def cli_restore(db, session_id):
    from hermes_cli.cli_model_routing import restore_class_route
    cli = SimpleNamespace(_explicit_model_override=False)
    assert restore_class_route(cli, db.get_session(session_id))
    assert cli._explicit_api_key is cli._explicit_base_url is None
    return cli.model, cli.requested_provider


def desktop_restore(db, session_id):
    from tui_gateway.class_routing import restore_class_overrides
    row = db.get_session(session_id)
    overrides = restore_class_overrides(row["model"], json.loads(row["model_config"]), {})
    assert overrides["model_override"]["provider"] == overrides["provider_override"]
    return overrides["model_override"]["model"], overrides["provider_override"]


def messaging_restore(db, session_id):
    from gateway.run_class_routing import class_session_runtime
    runner = SimpleNamespace(_session_db=db, session_store=SimpleNamespace(
        _entries={"key": SimpleNamespace(session_id=session_id)}))
    model, runtime = class_session_runtime(runner, "class:fast", None, None, {}, "key")
    assert runtime["provider"] == runtime["requested_provider"]
    return model, runtime["provider"]


def api_restore(db, session_id):
    from gateway.platforms.api_server_class_routing import prepare_runtime
    adapter = SimpleNamespace(_ensure_session_db=lambda: db, _session_model_override_for=lambda key: None)
    with patch("gateway.run._resolve_gateway_model", return_value="paid-default"), patch(
        "gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("default authentication")):
        _, runtime, model = prepare_runtime(adapter, session_id, db.get_session(session_id)["model"],
                                            False, None, None, None)
    assert runtime["provider"] == runtime["requested_provider"]
    return model, runtime["provider"]


@pytest.mark.parametrize("restore", [cli_restore, desktop_restore, messaging_restore, api_restore])
@pytest.mark.parametrize("exact", [True, False], ids=["pin", "class"])
@pytest.mark.parametrize("continuation", [False, True], ids=["root", "compression-child"])
def test_missing_allocation_db_preserves_resume_intent(
        routing_home, streaming_route, tmp_path, restore, exact, continuation):
    routing_home["model_routing"]["enforce_subscription_only"] = True
    second = deepcopy(routing_home["model_routing"]["routes"]["deep"][0])
    second["model"] = "z-pinned-model"
    routing_home["model_routing"]["routes"]["deep"].append(second)
    if exact:
        # Model name alone is ambiguous: dropping the provider must fail.
        other_provider = dict(second, provider="openai-codex")
        routing_home["model_routing"]["routes"]["deep"].append(other_provider)
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    original = resolve_runtime_provider(requested="anthropic" if exact else "auto",
        target_model="z-pinned-model" if exact else "class:deep", routing_session_id="root")
    path = tmp_path / "model-routing.sqlite3"
    state = RoutingState(path)
    assert state.peek("root", "deep") == original["model_routing"]
    session_id = "child" if continuation else "root"
    if continuation:
        state.link_continuation("child", "root")
        assert state.peek("child", "deep") == original["model_routing"]
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path=db_path)
    db.create_session(session_id, source="local", model=original["model"], model_config={
        "model_routing": original["model_routing"], "provider": "stale-provider"})
    db.close()
    path.unlink()
    db = SessionDB(db_path=db_path)
    try:
        model, provider = restore(db, session_id)
        assert not path.exists(), "Resume adapter must not allocate"
        assert (model, provider) == (("z-pinned-model", "anthropic") if exact else ("class:deep", "auto"))
        resumed = resolve_runtime_provider(requested=provider, target_model=model, routing_session_id=session_id)
        metadata = RoutingState(path).peek(session_id, "deep")
        assert metadata == resumed["model_routing"]
        assert bool(metadata.get("exact_pin")) is exact
        assert (metadata["selected_model"], metadata["selected_provider"]) == (original["model"], original["provider"])
        if exact:
            with pytest.raises(ModelRoutingError, match="cannot fail over"):
                RoutingState(path).allocate(session_id, None, "deep", [second], metadata["policy_version"],
                                            {("anthropic", "z-pinned-model")})
    finally:
        db.close()


@pytest.mark.parametrize("provider", [None, "", "auto"])
def test_incomplete_exact_tuple_fails_closed(provider):
    with pytest.raises(ModelRoutingError, match="concrete model/provider"):
        restore_route("pinned", {"model_routing": {
            "selected_model": "pinned", "selected_provider": provider, "requested_class": "deep", "exact_pin": True}})


def test_restored_pin_is_not_managed_authorization(routing_home, tmp_path):
    routing_home["model_routing"]["enforce_subscription_only"] = True
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    # A persisted pin the policy refuses is history: resume drops it (the session runs on its configured
    # default) instead of replaying it, and an explicit request for the same tuple still refuses unallocated.
    assert restore_route("paid", {"model_routing": {
        "selected_model": "paid", "selected_provider": "openrouter", "requested_class": "deep", "exact_pin": True}}) is None
    with pytest.raises(ModelRoutingError, match="rejects"):
        resolve_runtime_provider(requested="openrouter", target_model="paid", routing_session_id="stale")
    assert not (tmp_path / "model-routing.sqlite3").exists()
