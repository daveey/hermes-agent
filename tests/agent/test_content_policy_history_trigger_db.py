"""[daveey] History-triggered refusal through the real compressor and SessionDB (no _compress_context mock).

The mocked suites (test_content_policy_history_trigger*.py) never ran the system-prompt rebuild or the session
DB commit, so passing the full built prompt as the caller's system_message doubled the prompt unnoticed
(review of PR #1300). Harness: tests/agent/test_in_place_preflight_rewind.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

MARKER = "TRIGGER-7f3a"


def _aux_llm(**kwargs):
    text = "## Goal\nDeploy questions.\n## Progress\nDiscussed targets." if kwargs.get("task") == "compression" else "Title"
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], model="aux", usage=None)


def _reply(content=None, refusal=None):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning=None, reasoning_content=None, refusal=refusal)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], model="test/model",
                               id="resp")
    response.usage = SimpleNamespace(prompt_tokens=5000, completion_tokens=100, total_tokens=5100)
    return response


@pytest.fixture
def session(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from run_agent import AIAgent

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("agent.context_compressor.call_llm", _aux_llm)
    monkeypatch.setattr("agent.title_generator.call_llm", _aux_llm)
    db = SessionDB(db_path=tmp_path / "state.db")
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", model="test/model",
                        quiet_mode=True, session_db=db, session_id="sid", skip_context_files=True, skip_memory=True)
    agent.client, agent.tool_delay, agent.save_trajectories = MagicMock(), 0, False
    yield db, agent
    db.close()


def _gateway_turn(db, agent, text, provider):
    from gateway.run import _build_gateway_agent_history

    agent.client.chat.completions.create.side_effect = provider
    history = _build_gateway_agent_history(db.get_messages_as_conversation("sid", repair_alternation=True))[0]
    return agent.run_conversation(user_message=text, conversation_history=history, task_id="sid",
                                  persist_user_message=text)


def test_history_refusal_retry_keeps_system_prompt_and_one_current_row(session):
    db, agent = session
    # The trigger sits in an early assistant reply, older than the lean tail the compressor keeps.
    for n in range(1, 13):
        extra = f" {MARKER}" if n == 2 else ""
        body = f"A{n} answer{extra} " + "lorem ipsum dolor " * 400
        _gateway_turn(db, agent, f"U{n} question", lambda body=body, **kw: _reply(content=body))
    prompt_before = db.get_session("sid").get("system_prompt") or ""
    cached_before = agent._cached_system_prompt
    assert prompt_before and cached_before

    calls = []

    def provider(**kw):
        calls.append(kw)
        if any(MARKER in str(m.get("content") or "") for m in kw.get("messages") or []):
            return _reply(refusal="I can't continue this conversation.")
        return _reply(content="A13 It runs on the hub.")

    result = _gateway_turn(db, agent, "U13 does it run on the mini or the hub?", provider)

    assert len(calls) == 2  # refused, then the compacted retry answered
    assert result.get("final_response") == "A13 It runs on the hub."
    assert agent._cached_system_prompt == cached_before
    assert (db.get_session("sid").get("system_prompt") or "") == prompt_before
    model_history, display = db.get_resume_conversations("sid")
    live = [m.get("content") or "" for m in model_history if isinstance(m.get("content"), str)]
    shown = [m.get("content") or "" for m in display if isinstance(m.get("content"), str)]
    assert sum(c.startswith("U13 ") for c in live) == 1
    assert sum(c.startswith("U13 ") for c in shown) == 1
    assert not any(MARKER in c for c in live)


def test_history_refusal_retry_rotation_mode_sends_current_once(session):
    """Legacy ``compression.in_place: false``: the compressor's head already ends in the current message
    (merged into the user-turn anchor), so appending it again sent it 3x and stored it doubled."""
    db, agent = session
    agent.compression_in_place = False
    for n in range(1, 13):
        extra = f" {MARKER}" if n == 2 else ""
        body = f"A{n} answer{extra} " + "lorem ipsum dolor " * 400
        _gateway_turn(db, agent, f"U{n} question", lambda body=body, **kw: _reply(content=body))

    calls = []

    def provider(**kw):
        calls.append(kw)
        if any(MARKER in str(m.get("content") or "") for m in kw.get("messages") or []):
            return _reply(refusal="I can't continue this conversation.")
        return _reply(content="A13 It runs on the hub.")

    current = "U13 does it run on the mini or the hub?"
    result = _gateway_turn(db, agent, current, provider)

    assert len(calls) == 2
    assert result.get("final_response") == "A13 It runs on the hub."
    retry_users = [m for m in calls[-1]["messages"] if m.get("role") == "user"]
    assert retry_users[-1]["content"] == current
    sent = "\n".join(str(m.get("content") or "") for m in calls[-1]["messages"])
    assert sent.count(current) == 1
    child = agent.session_id
    assert child != "sid"  # rotation mode moved the turn to a child session
    rows = [m.get("content") or "" for m in db.get_messages_as_conversation(child)
            if isinstance(m.get("content"), str)]
    assert "\n".join(rows).count(current) == 1
