"""First assistant flush diagnostic contract; disposable store, no provider or live chat."""
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import hermes_state_lease_custody as custody
from tests.tui_gateway.test_cli_takeover_full_turn import make_agent

LOGGER = "hermes_state.lease_custody"


@pytest.mark.parametrize("state", ["valid", "missing", "mismatch"])
@pytest.mark.parametrize("tool_gate", [False, True])
def test_first_assistant_flush_custody(tmp_path, monkeypatch, caplog, state, tool_gate):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    caplog.set_level(logging.INFO, logger=LOGGER)
    agent = make_agent(tmp_path, "desktop")
    db = agent._session_db
    sid = "handoff"
    owner = f"pid={os.getpid()}:turn=private-owner"
    presented = owner if state != "mismatch" else f"pid={os.getpid()}:turn=private-other"
    content = "private-first-assistant-content"
    try:
        db.create_session(sid, source="desktop")
        assert db.try_acquire_session_turn_lease(sid, owner)
        assert db.refresh_session_turn_lease(sid, owner)
        if state == "missing":
            db.release_session_turn_lease(sid, owner)
        agent._active_session_turn_lease_holder = presented
        messages = [] if tool_gate else [{"role": "assistant", "content": content}]
        if tool_gate:
            _first_tool_gate(agent, messages, content, state, monkeypatch)
        else:
            assert agent._flush_messages_to_session_db(messages, []) is (state == "valid")
        rows = db.get_messages(sid)
        assert [r["content"] for r in rows] == ([content] if state == "valid" else [])
        if state == "valid":
            assert agent._flush_messages_to_session_db(messages, [])
            assert len(db.get_messages(sid)) == 1
        else:
            assert agent._last_persistence_error_cause == "turn_lease"
        events = [dict(p.split("=", 1) for p in r.getMessage().split()[1:])
                  for r in caplog.records if r.name == LOGGER]
        claim = next(e for e in events if e.get("op") == "claim")
        assert claim["holder"] == custody.tag(owner)
        assert claim["txn"].startswith(str(os.getpid()) + ".")
        if state != "valid":
            refused = next(e for e in events if e.get("op") == state)
            assert refused["store"] == claim["store"]
            assert refused["key"] == claim["key"]
            assert refused["holder"] == custody.tag(presented)
            assert refused["row_holder"] == ("-" if state == "missing" else custody.tag(owner))
            assert any(e["phase"] == "rolled_back" and e["txn"] == refused["txn"] for e in events)
        actual = db.session_turn_lease_owner(sid)
        assert (actual is None) == (state == "missing")
        if actual:
            assert actual["holder"] == owner
        text = "\n".join(r.getMessage() for r in caplog.records if r.name == LOGGER)
        for secret in (owner, presented, content, str(tmp_path)):
            assert secret not in text
        # Prospective provenance gap: each real refresh and mutation must name its caller.
        refresh = next(e for e in events if e.get("op") == "refresh")
        assert refresh["changed"] == "1"
        for event in (claim, refresh):
            assert "test_first_assistant_flush_custody.py:test_first_assistant_flush_custody:" in event["caller"]
            assert len(event["caller"].split(">")) <= 12
            assert any(e["phase"] == "committed" and e["txn"] == event["txn"] for e in events)
        assert "refresh_session_turn_lease:" in refresh["caller"]
        if state == "missing":
            release = next(e for e in events if e.get("op") == "release")
            assert "release_session_turn_lease:" in release["caller"]
        if state != "valid":
            assert not db.refresh_session_turn_lease(sid, presented)
            zero = [r.getMessage() for r in caplog.records if r.name == LOGGER][-2]
            assert "op=refresh" in zero and "changed=0" in zero
            assert not db.get_messages(sid)
    finally:
        agent._active_session_turn_lease_holder = None
        agent.close()


def _first_tool_gate(agent, messages, content, state, monkeypatch):
    from agent.turn_tool_round import run_tool_round

    class ReachedDispatcher(Exception):
        pass

    agent.valid_tool_names = {"skill_view"}
    message = SimpleNamespace(content=content, tool_calls=[SimpleNamespace(
        id="call_fixture", type="function", function=SimpleNamespace(name="skill_view", arguments='{"name":"fixture"}'))],
        reasoning=None, reasoning_content=None, reasoning_details=None)
    dispatch = Mock(side_effect=ReachedDispatcher)
    interim = Mock()
    monkeypatch.setattr(agent, "_execute_tool_calls", dispatch)
    monkeypatch.setattr(agent, "_emit_interim_assistant_message", interim)
    monkeypatch.setattr("agent.prepared_compaction.maybe_prepare", Mock())
    args = dict(assistant_message=message, finish_reason="tool_calls", messages=messages,
                conversation_history=[], api_call_count=1, effective_task_id="fixture", user_message="read",
                system_message="", active_system_prompt="", compression_attempts=0, max_compression_attempts=1,
                final_response=None, failed=False, _turn_exit_reason=None, truncated_tool_call_retries=0,
                current_turn_user_idx=0)
    if state == "valid":
        with pytest.raises(ReachedDispatcher):
            run_tool_round(agent, **args)
        dispatch.assert_called_once()
        interim.assert_called_once()
    else:
        verdict = run_tool_round(agent, **args)
        assert verdict.action == "break" and verdict.failed
        assert verdict._turn_exit_reason == "session_persistence_failed"
        dispatch.assert_not_called()
        interim.assert_not_called()
