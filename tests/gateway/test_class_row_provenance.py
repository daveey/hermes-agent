"""A gateway session row created before the agent keeps the agent's class provenance.

The gateway SessionStore writes a bare row (no model_config) when a message arrives. The agent's turn
lease then sees that row and marks it created, so the agent's own lazy create (the only writer of its
init model_config: model_routing, max_iterations, ...) never ran. The row then looked like a concrete
pin to ``stored_route`` after a gateway restart.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from run_agent import AIAgent
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from gateway.session import Platform, SessionSource
from tests.gateway.test_pre_agent_fallback_notice import _runner_with_real_runtime_resolution
from tests.agent import test_model_routing_agent
from tests.tui_gateway import test_class_routing

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route
native_transport = test_class_routing.native_transport


def _row_config(db, session_id):
    return json.loads(db.get_session(session_id)["model_config"] or "{}")


def test_gateway_bare_row_gets_class_provenance(routing_home, streaming_route, native_transport):
    from hermes_state import SessionDB
    from hermes_constants import get_hermes_home
    from agent.model_routing_state import RoutingState
    from gateway.run_class_routing import stored_route
    from gateway.run_turn import GatewayTurnMixin

    db = SessionDB()
    runner = _runner_with_real_runtime_resolution()
    runner._session_db = db
    session_id, session_key = "gw-class-row", "test-session-key"
    runner.session_store._entries = {session_key: SimpleNamespace(session_id=session_id)}
    source = SessionSource(platform=Platform.SLACK, chat_id="c", user_id="u")
    # The gateway SessionStore's bare row (gateway/session_recovery.py::_db_create_kwargs).
    db.create_session(session_id=session_id, source="slack", user_id="u", session_key=session_key,
                      chat_id="c", model_config=None)
    ctx = TurnContext(
        source=source, message="hi", history=[], session_id=session_id, session_key=session_key,
        user_config=routing_home, AIAgent=AIAgent,
        resolve_display_setting=lambda *_a: False, _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    with patch("gateway.run._resolve_gateway_model", return_value="class:deep"), \
            patch("gateway.run._get_channel_override", return_value=None), \
            patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("legacy billing bypass")), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=routing_home)
        assert model == "class:deep"
        agent = TurnRunner(runner, ctx)._build_fresh_agent(
            {"model": model, "runtime": runtime}, "slack", "", 5, None, {}, True)
        try:
            assert agent.run_conversation("Say ok")["final_response"] == "ok"
            # The post-turn gateway writer (gateway_runtime) must merge, not replace.
            GatewayTurnMixin._sync_session_model_from_agent(
                SimpleNamespace(_session_db=SimpleNamespace(_db=db)), session_id, agent)
        finally:
            agent.release_clients()

    allocation = RoutingState(get_hermes_home() / "model-routing.sqlite3").peek(session_id, "deep")
    assert allocation and allocation["requested_class"] == "deep"
    config = _row_config(db, session_id)
    assert config.get("model_routing") == allocation
    assert config.get("max_iterations") == agent.max_iterations
    assert config["gateway_runtime"]["provider"] == "anthropic"
    # Gateway restart: the stored row resumes the class, not a concrete pin.
    assert stored_route(runner, session_key) is not None
    assert stored_route(runner, session_key)[0] == "class:deep"
    db.close()


def test_existing_row_keys_are_never_overwritten(routing_home, streaming_route, native_transport):
    """Seeding only fills keys the row lacks: an earlier writer's value (an explicit pin's cleared
    route, a gateway_runtime) wins over the agent's init config."""
    from hermes_state import SessionDB

    db = SessionDB()
    db.create_session(session_id="gw-class-keep", source="slack", model_config={
        "max_iterations": 7, "gateway_runtime": {"provider": "earlier"}})
    with patch("model_tools.get_tool_definitions", return_value=[]):
        agent = AIAgent(model="class:deep", provider="auto", session_id="gw-class-keep", session_db=db,
                        skip_context_files=True, skip_memory=True, quiet_mode=True, max_iterations=5)
        try:
            agent.run_conversation("Say ok")
        finally:
            agent.release_clients()
    config = _row_config(db, "gw-class-keep")
    assert config["max_iterations"] == 7
    assert config["gateway_runtime"] == {"provider": "earlier"}
    assert config["model_routing"]["requested_class"] == "deep"
    db.close()
