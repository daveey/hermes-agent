"""A writer outliving SessionDB's 60s patience must pause, not kill, a tool turn."""
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

import agent.session_persistence as sp
from agent.session_persistence import SessionPersistenceMixin
from tests.agent.test_tool_call_incremental_persistence import (
    _attach_real_session_db,
    _make_agent,
    _mock_response,
    _mock_tool_call,
)


def test_multistep_turn_survives_writer_longer_than_sixty_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_LOCKED_FLUSH_RETRY_BUDGET_OVERRIDE_S", 600.0)
    agent = _make_agent()
    path = tmp_path / "state.db"
    db = _attach_real_session_db(agent, path, "long-writer")
    ready, release = threading.Event(), threading.Event()
    held_for, executed = [], []

    def writer():
        conn = sqlite3.connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            ready.set()
            release.wait(65)
            held_for.append(time.monotonic() - start)
            conn.rollback()
        finally:
            conn.close()

    worker = threading.Thread(target=writer)
    responses = iter([
        _mock_response("", "tool_calls", [_mock_tool_call(call_id="c1")]),
        _mock_response("", "tool_calls", [_mock_tool_call(call_id="c2")]),
        _mock_response("done"),
    ])
    requests = []

    def respond(**kwargs):
        requests.append(kwargs)
        if len(requests) == 2:
            worker.start()
            assert ready.wait(10)
        return next(responses)

    def execute(assistant_message, messages, effective_task_id, api_call_count=0):
        call_id = assistant_message.tool_calls[0].id
        executed.append(call_id)
        messages.append({"role": "tool", "tool_call_id": call_id, "name": "web_search", "content": call_id})
        assert agent._flush_messages_to_session_db(messages) is True

    agent.client.chat.completions.create.side_effect = respond
    try:
        with (
            patch.object(agent, "_execute_tool_calls", side_effect=execute),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.title_generator.maybe_auto_title"),
        ):
            result = agent.run_conversation("perform two steps")
        assert result["final_response"] == "done"
        assert executed == ["c1", "c2"]
        assert held_for[0] > 60
        rows = db.get_messages_as_conversation("long-writer")
        assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
        assert [row["tool_call_id"] for row in rows if row["role"] == "tool"] == ["c1", "c2"]
        assert agent._flush_messages_to_session_db(result["messages"]) is True
        assert db.get_messages_as_conversation("long-writer") == rows
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(10)
        db.close()


@pytest.mark.parametrize("cause", ["disk", "corrupt", "turn_lease", "unknown"])
def test_non_lock_failure_still_fails_closed(cause):
    class Agent(SessionPersistenceMixin):
        def _flush_messages_to_session_db_unlocked(self, *args):
            self._last_persistence_error_cause = cause
            return False

    assert Agent()._flush_messages_to_session_db([]) is False


def test_lock_retry_stops_when_interrupted(monkeypatch):
    monkeypatch.setattr(sp, "_LOCKED_FLUSH_RETRY_BUDGET_OVERRIDE_S", 600.0)
    class Agent(SessionPersistenceMixin):
        _interrupt_requested = True

        def _flush_messages_to_session_db_unlocked(self, *args):
            self._last_persistence_error_cause = "locked"
            return False

    assert Agent()._flush_messages_to_session_db([]) is False


def test_lock_retry_gives_up_after_budget(monkeypatch):
    monkeypatch.setattr(sp, "_LOCKED_FLUSH_RETRY_BUDGET_OVERRIDE_S", 0.0)
    calls = []

    class Agent(SessionPersistenceMixin):
        def _flush_messages_to_session_db_unlocked(self, *args):
            calls.append(1)
            self._last_persistence_error_cause = "locked"
            return False

    assert Agent()._flush_messages_to_session_db([]) is False
    assert len(calls) == 1


def test_lock_retry_succeeds_once_writer_releases(monkeypatch):
    monkeypatch.setattr(sp, "_LOCKED_FLUSH_RETRY_BUDGET_OVERRIDE_S", 600.0)
    attempts = []

    class Agent(SessionPersistenceMixin):
        def _flush_messages_to_session_db_unlocked(self, *args):
            attempts.append(1)
            if len(attempts) < 3:
                self._last_persistence_error_cause = "locked"
                return False
            return True

    assert Agent()._flush_messages_to_session_db([]) is True
    assert len(attempts) == 3


def test_budget_defaults(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    assert sp._locked_flush_retry_budget_s() == 0.0
    monkeypatch.delenv("HERMES_TEST_ISOLATION")
    assert sp._locked_flush_retry_budget_s() == sp._LOCKED_FLUSH_RETRY_BUDGET_S == 600.0
