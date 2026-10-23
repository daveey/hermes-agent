"""Compression publication preserves the allocator identity across restarts."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_compression import _publish_rotated_compaction
from agent.model_routing import resolve_model_route
from hermes_state import SessionDB
from tests.agent import test_model_routing_agent as agent_tests

build_agent = agent_tests.build_agent
routing_home = agent_tests.routing_home
streaming_route = agent_tests.streaming_route


def counters(home):
    with sqlite3.connect(home / "model-routing.sqlite3") as db:
        return db.execute("SELECT pool, position FROM counters ORDER BY pool").fetchall()


def rotate(agent):
    old = agent.session_id
    _publish_rotated_compaction(
        agent, [], [{"role": "user", "content": "compacted history"}],
        new_system_prompt="stable after compression", old_session_id=old,
        lease=SimpleNamespace(holder=None, ttl=60, watermark=None),
        compressed_user_turn_outcome="already_present",
    )
    return agent.session_id


def test_rotated_class_keeps_allocation_across_two_hops_and_restart(routing_home, streaming_route, tmp_path):
    with SessionDB(tmp_path / "state.db") as db, \
         patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        agent = build_agent(session_id="root", session_db=db)
        db.create_session("root", source="cli", model=agent.model,
                          model_config=agent._session_init_model_config)
        agent._session_db_created = True
        expected = dict(agent.model_routing)
        before = counters(tmp_path)
        for _ in range(2):
            child = rotate(agent)
            restarted = build_agent(session_id=child, session_db=db)
            assert restarted.model_routing == expected
            assert counters(tmp_path) == before
        from agent.conversation_compression import _adopt_live_compression_child
        stale = build_agent(session_id="root", session_db=db)
        assert _adopt_live_compression_child(stale, db, "root")
        assert stale.session_id == child
        assert stale.model_routing == expected
        assert counters(tmp_path) == before
        with sqlite3.connect(tmp_path / "model-routing.sqlite3") as state:
            assert state.execute("SELECT root FROM aliases WHERE session=?", (child,)).fetchone() == ("root",)


def test_failed_publication_cannot_change_parent_route(routing_home, streaming_route, tmp_path):
    with SessionDB(tmp_path / "state.db") as db, \
         patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        agent = build_agent(session_id="root", session_db=db)
        db.create_session("root", source="cli", model=agent.model)
        expected = dict(agent.model_routing)
        before = counters(tmp_path)
        with patch.object(db, "publish_compression_child", side_effect=RuntimeError("publish failed")):
            with pytest.raises(RuntimeError, match="publish failed"):
                rotate(agent)
        assert agent.session_id == "root"
        assert build_agent(session_id="root").model_routing == expected
        assert counters(tmp_path) == before


def test_lineage_link_rejects_existing_independent_allocation(tmp_path):
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_state import RoutingState
    from tests.agent.test_model_routing import policy
    path = tmp_path / "routes.db"
    for session in ("parent", "independent"):
        resolve_model_route("class:deep", config=policy(), session_id=session, state_path=path)
    with pytest.raises(ModelRoutingError, match="lineage"):
        RoutingState(path).link_continuation("independent", "parent")


def test_lineage_link_is_idempotent_and_does_not_allocate(tmp_path):
    from agent.model_routing_state import RoutingState
    from tests.agent.test_model_routing import policy
    path = tmp_path / "routes.db"
    expected = resolve_model_route("class:deep", config=policy(), session_id="parent", state_path=path)
    state = RoutingState(path)
    state.link_continuation("child", "parent")
    state.link_continuation("child", "parent")
    state.link_continuation("tip", "child")
    assert resolve_model_route("class:deep", config=policy(), session_id="tip", state_path=path) == expected
