"""An exact global pin must not resurrect a class after cache eviction/restart."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.run import GatewayRunner
from gateway.run_class_routing import stored_class
from gateway.slash_commands_model import _ModelSwitchContext
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
    result = SimpleNamespace(new_model="selected-model", target_provider="anthropic", api_key="",
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
