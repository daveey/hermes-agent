"""Compression publication must distinguish held steering from foreign appends."""
import copy

import pytest
from hermes_state import SessionDB
from agent.prompt_builder import steer_user_row

pytest_plugins = ["tests.agent.test_pre_final_gate"]


@pytest.mark.parametrize("in_place", [False, True])
def test_compaction_does_not_replay_own_preflushed_rows(agent, tmp_path, monkeypatch, in_place):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.conversation_compression_tools.refresh_agent_tool_definitions", lambda _: False)
    db = SessionDB(tmp_path / "state.db")
    agent._session_db = db
    agent._ensure_db_session()
    db.set_session_title(agent.session_id, "Isolated steering publication test")
    parent = agent.session_id
    first = {"role": "user", "content": "Repair example"}
    agent._persist_user_message_idx = 0
    assert agent._flush_messages_to_session_db([first])
    steer = steer_user_row("Do not publish")
    rows = [first, {"role": "assistant", "content": "Old intermediate answer"}, steer]
    agent._session_messages = rows
    agent.compression_in_place = in_place

    def summarize(messages, **kwargs):
        # A real other writer appends AFTER the lease snapshot. It must survive.
        db.append_message(parent, "user", "Concurrent new request",
                          compression_lock_holder=agent._active_compression_lock_holder)
        return [copy.deepcopy(messages[0]), {"role": "assistant", "content": "Summary of earlier work"}]

    monkeypatch.setattr(agent.context_compressor, "compress", summarize)
    try:
        agent._compress_context(rows, agent._cached_system_prompt, force=True)
        assert (agent.session_id == parent) is in_place
        live = db.get_messages(agent.session_id)
        contents = [r["content"] for r in live]
        assert "Concurrent new request" in contents
        assert "Old intermediate answer" not in contents, contents
        assert steer["content"] not in contents, contents
        archive = db.get_messages(parent, include_inactive=True)
        assert sum(r.get("message_uid") == steer["message_uid"] for r in archive) == 1
    finally:
        db.close()


@pytest.mark.parametrize("in_place", [False, True])
def test_lost_compression_lease_does_not_publish_pending_steering(agent, tmp_path, monkeypatch, in_place):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.conversation_compression_tools.refresh_agent_tool_definitions", lambda _: False)
    db = SessionDB(tmp_path / "state.db")
    agent._session_db = db
    agent._ensure_db_session()
    db.set_session_title(agent.session_id, "Isolated lease test")
    parent = agent.session_id
    first = {"role": "user", "content": "Repair example"}
    agent._persist_user_message_idx = 0
    assert agent._flush_messages_to_session_db([first])
    rows = [first, {"role": "assistant", "content": "Checking"}, steer_user_row("Do not publish")]
    agent._session_messages = rows
    agent.compression_in_place = in_place
    def summarize(messages, **kwargs):
        db.release_compression_lock(parent, agent._active_compression_lock_holder)
        assert db.try_acquire_compression_lock(parent, "replacement-owner", ttl_seconds=300)
        return [copy.deepcopy(messages[0]), {"role": "assistant", "content": "Forbidden summary publication"}]
    monkeypatch.setattr(agent.context_compressor, "compress", summarize)
    try:
        returned, _ = agent._compress_context(rows, agent._cached_system_prompt, force=True)
        assert agent.session_id == parent
        assert "Forbidden summary publication" not in str(returned)
        assert "Forbidden summary publication" not in str(db.get_messages(parent))
        assert rows[-1]["content"] in [r.get("content") for r in returned]
        assert db.get_compression_lock_holder(parent) == "replacement-owner"
    finally:
        db.close()


def test_provider_owned_compaction_retains_live_steering(agent, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent.steering_provenance import steering_content
    from agent.transports.codex_app_server_session import TurnResult
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    row = steer_user_row("Do not publish")
    rows = [{"role": "user", "content": "Repair example"},
            {"role": "assistant", "content": "Checking"}, row]
    calls = []
    def compact():
        calls.append(True)
        return TurnResult(thread_id="isolated-thread", turn_id="compact-turn")
    agent.api_mode = "codex_app_server"
    agent._codex_session = SimpleNamespace(compact_thread=compact)
    returned, _ = agent._compress_context(rows, agent._cached_system_prompt, force=True)
    assert calls == [True]
    assert returned == rows
    assert steering_content(returned[-1]) == "Do not publish"
