"""Accepted steering must be durable before compaction can drop it."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.prompt_builder import steer_user_row
from agent.steering_provenance import flush_steering_before_compaction


@pytest.mark.parametrize("storage", ["missing", "failure"])
def test_compaction_refuses_to_lose_unpersisted_steering(storage):
    row = steer_user_row("Do not publish")
    agent = SimpleNamespace(_session_db=None if storage == "missing" else object(),
                            _persist_user_message_idx=0,
                            _flush_messages_to_session_db=Mock(return_value=False))
    with pytest.raises(RuntimeError, match="steering"):
        flush_steering_before_compaction(agent, [row])


def test_compaction_flushes_once_through_native_identity_boundary():
    row = steer_user_row("Do not publish")
    history = {"role": "user", "content": "Earlier request"}
    def flush(messages, conversation_history):
        assert messages == [history, row]
        assert conversation_history == [history]
        row["_db_persisted"] = True
        return True
    agent = SimpleNamespace(_session_db=object(), _persist_user_message_idx=1,
                            _flush_messages_to_session_db=Mock(side_effect=flush))
    flush_steering_before_compaction(agent, [history, row])
    flush_steering_before_compaction(agent, [history, row])
    agent._flush_messages_to_session_db.assert_called_once()
