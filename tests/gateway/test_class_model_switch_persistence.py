"""An exact global pin must not resurrect a class after cache eviction/restart."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.run import GatewayRunner
from gateway.run_class_routing import stored_class
from gateway.slash_commands_model import _ModelSwitchContext
from hermes_cli.model_switch import ModelSwitchResult
from hermes_state import AsyncSessionDB, SessionDB


@pytest.mark.asyncio
@pytest.mark.parametrize("picker", [False, True])
async def test_same_model_global_pin_clears_class_without_cached_agent(tmp_path, monkeypatch, picker):
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path=db_path)
    metadata = {"requested_class": "deep", "selected_model": "selected-model"}
    db.create_session("s", source="local", model="selected-model", model_config={
        "model_routing": metadata, "_branched_from": "parent", "unrelated": "keep",
    })
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = SimpleNamespace(_entries={"key": SimpleNamespace(session_id="s")})
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="s")),
        set_model_override=AsyncMock(),
    )
    runner._session_model_overrides = {}
    runner._evict_cached_agent = Mock()
    runner._cached_agent_for = Mock(return_value=None)
    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", AsyncMock())
    ctx = _ModelSwitchContext(session_key="key", source=None, config_path=None,
                              persist_global=True, current_model="selected-model")
    result = ModelSwitchResult(success=True, new_model="selected-model", target_provider="anthropic", api_key="",
                             base_url="", api_mode="anthropic_messages", provider_label="Anthropic",
                             request_overrides={}, runtime_capabilities={})
    assert stored_class(runner, "key") == "class:deep"
    assert runner._switch_cached_agent_model(result, ctx, picker) is None
    assert await runner._record_model_switch(result, ctx, source=None, one_turn=False, picker=picker) is None
    assert not runner._session_model_overrides
    db.close()
    reopened = SessionDB(db_path=db_path)
    try:
        runner._session_db = AsyncSessionDB(reopened)
        assert stored_class(runner, "key") is None
        config = json.loads(reopened.get_session("s")["model_config"])
        assert "model_routing" not in config
        assert config["_branched_from"] == "parent"
        assert config["unrelated"] == "keep"
        assert config["provider"] == "anthropic"
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_once_pin_preserves_durable_class_before_and_after_restore(tmp_path, monkeypatch, cached):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    metadata = {"requested_class": "deep", "selected_model": "selected-model"}
    db.create_session("s", source="local", model="selected-model", model_config={"model_routing": metadata})
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = SimpleNamespace(_entries={"key": SimpleNamespace(session_id="s")})
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="s")),
        set_model_override=AsyncMock(),
    )
    runner._session_model_overrides = {}
    runner._evict_cached_agent = Mock()
    agent = SimpleNamespace(model_routing=metadata)

    def switch(**kwargs):
        # The common agent switch deliberately clears provenance for permanent pins.
        db.patch_session_model_config("s", {"model_routing": None})
        agent.model_routing = None

    agent.switch_model = Mock(side_effect=switch)
    runner._cached_agent_for = Mock(return_value=agent if cached else None)
    ctx = _ModelSwitchContext(session_key="key", source=None, config_path=None,
                              persist_global=False, one_turn=True, current_model="selected-model",
                              restore_snapshot=runner._snapshot_session_model_override("key"))
    result = ModelSwitchResult(success=True, new_model="temporary-model", target_provider="anthropic", api_key="",
                             base_url="", api_mode="anthropic_messages", provider_label="Anthropic",
                             request_overrides={}, runtime_capabilities={})
    try:
        assert runner._switch_cached_agent_model(result, ctx, False) is None
        assert await runner._record_model_switch(result, ctx, source=None, one_turn=True, picker=False) is None
        assert runner._session_model_overrides["key"]["model"] == "temporary-model"
        runner._async_session_store.set_model_override.assert_not_awaited()
        # Crash/restart before a temporary turn must retain the standing allocation.
        assert stored_class(runner, "key") == "class:deep"
        assert db.get_session("s")["model"] == "selected-model"
        assert json.loads(db.get_session("s")["model_config"])["model_routing"] == metadata
        runner._restore_pending_one_turn_model_override("key")
        assert not runner._session_model_overrides
        assert stored_class(runner, "key") == "class:deep"
        if cached:
            agent.switch_model.assert_not_called()
    finally:
        db.close()
