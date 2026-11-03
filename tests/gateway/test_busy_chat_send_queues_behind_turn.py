"""A send to a busy Desktop chat that reaches the gateway is queued behind the running turn, not refused.

Live (MacBook, 2026-11-02 21:44 PST): Desktop's "turn it back on", typed while another writer's turn held
the chat, was forwarded to the owner gateway's FIFO and ran there as ``gateway.run: inbound message:
platform=local``. That gateway turn's agent has the session's surface as its platform (``desktop``,
``session_policy.SURFACES['gui']``), so ``admit_durable_turn_lease`` gave it the Desktop serve rule,
``wait_seconds=0``: it failed ``session_busy`` at once and the message was lost ("Couldn't send your
message"). A cross_session relay at 21:53 died the same way behind an auto kanban_notice turn.

Real: the gateway's agent build (``TurnRunner._build_fresh_agent`` with the ``desktop`` platform key a
GUI local session carries), ``AIAgent.run_conversation`` with its durable turn lease, the SessionDB lease
table and the transcript. Replaced: only the provider wire (``native_transport``: answers "ok").
"""
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from hermes_state import SessionDB
from hermes_state_pidns import holder_namespace_token
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from gateway.session import Platform, SessionSource
from run_agent import AIAgent
from tests.agent import test_model_routing_agent
from tests.gateway.test_pre_agent_fallback_notice import _runner_with_real_runtime_resolution
from tests.tui_gateway import test_class_routing

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route
native_transport = test_class_routing.native_transport

SID = "20261026_235722_bef6c6"


def _running_turn(db, turn):
    """Another writer's live turn on the chat (this process's pid, so it is never reaped as dead)."""
    holder = f"pid={os.getpid()}{holder_namespace_token()}:turn={turn}:platform=desktop"
    assert db.try_acquire_session_turn_lease(SID, holder, ttl_seconds=300)
    return holder


def _gateway_turn(db, routing_home):
    """The gateway's own agent for a Desktop (gui) local chat, built the way the FIFO turn builds it."""
    runner = _runner_with_real_runtime_resolution()
    runner._session_db = db
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="local-chat", user_id="desktop"),
        message="", history=[], session_id=SID, session_key="agent:main:local:dm:local-chat",
        user_config=routing_home, AIAgent=AIAgent,
        resolve_display_setting=lambda *_a: False, _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    with patch("gateway.run._resolve_gateway_model", return_value="class:deep"), \
            patch("gateway.run._get_channel_override", return_value=None):
        model, runtime = runner._resolve_session_agent_runtime(source=ctx.source, user_config=routing_home)
    agent = TurnRunner(runner, ctx)._build_fresh_agent(
        {"model": model, "runtime": runtime}, "desktop", "", 5, None, {}, True)
    assert agent.platform == "desktop"
    return agent


def _send(agent, text):
    """Run the turn on a thread (the gateway runs it in an executor); returns (thread, outcome)."""
    outcome = {}

    def run():
        outcome["result"] = agent.run_conversation(text)
        outcome["at"] = time.monotonic()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _user_rows(db):
    return [m["content"] for m in db.get_messages(SID) if m["role"] == "user"]


def _assert_queued_then_processed(db, agent, holder, text):
    with patch("model_tools.get_tool_definitions", return_value=[]):
        thread, outcome = _send(agent, text)
        thread.join(2.5)  # a refusing admission ends the turn at once; a queued one is still waiting
        assert thread.is_alive(), f"refused while the turn ran: {outcome.get('result', {}).get('final_response')}"
        assert db.session_turn_lease_owner(SID)["holder"] == holder, "the running turn kept the chat"
        assert text not in _user_rows(db), "nothing of the queued send ran beside the running turn"
        ended = time.monotonic()
        db.release_session_turn_lease(SID, holder)  # the running turn ends
        thread.join(30)
    assert not thread.is_alive()
    result = outcome["result"]
    assert not result.get("failed"), result.get("final_response")
    assert result.get("failure_reason") != "session_busy"
    assert result["final_response"] == "ok"
    assert outcome["at"] >= ended, "it ran after the turn ended"
    assert _user_rows(db).count(text) == 1, "processed exactly once, in the stored transcript"
    assert db.session_turn_lease_owner(SID) is None
    agent.release_clients()


def test_desktop_send_while_a_turn_runs_is_processed_after_it(routing_home, native_transport, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SID, source="desktop")
    holder = _running_turn(db, "45b74d93")
    _assert_queued_then_processed(db, _gateway_turn(db, routing_home), holder, "turn it back on")


def test_cross_session_send_while_an_automation_turn_runs_is_processed_after_it(
        routing_home, native_transport, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SID, source="desktop")
    holder = _running_turn(db, "kanban_notice")  # the auto process_complete / kanban_notice turn
    relay = "[cross_session from @session:default@macbook/20261102_215300_aaaaaa] turn it back on"
    _assert_queued_then_processed(db, _gateway_turn(db, routing_home), holder, relay)


def test_control_idle_chat_runs_at_once(routing_home, native_transport, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SID, source="desktop")
    agent = _gateway_turn(db, routing_home)
    started = time.monotonic()
    with patch("model_tools.get_tool_definitions", return_value=[]):
        result = agent.run_conversation("turn it back on")
    assert result["final_response"] == "ok" and not result.get("failed")
    assert time.monotonic() - started < 2.0, "an idle chat never waits on the lease poll"
    assert _user_rows(db) == ["turn it back on"]
    agent.release_clients()


def test_control_serve_desktop_turn_still_refuses_a_held_lease_at_once(tmp_path):
    """A Desktop serve turn its own client submitted (no gateway key) keeps the instant refusal: that
    client still holds the text and offers Take over."""
    from agent.turn_facade_lease import admit_durable_turn_lease
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SID, source="desktop")
    _running_turn(db, "other-window")
    agent = SimpleNamespace(_session_db=db, session_id=SID, platform="desktop", _persist_disabled=False,
                            _interrupt_requested=False, statuses=[])
    agent._emit_status = agent._emit_warning = agent.statuses.append
    started = time.monotonic()
    admission = admit_durable_turn_lease(
        agent, session_id=SID, relay_turn_id=f"{SID}:t:x",
        task_context={"session_id": SID, "task_id": "t", "platform": "desktop"}, conversation_history=[])
    assert admission.lease is None and admission.early_result is not None
    assert admission.early_result["failure_reason"] == "session_busy"
    assert "Take over here" in admission.early_result["final_response"]
    assert time.monotonic() - started < 2.0
