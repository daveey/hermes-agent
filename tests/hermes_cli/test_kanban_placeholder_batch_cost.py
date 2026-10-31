"""Batch placeholder classification must not rediscover config for every chat link."""
import sqlite3
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_sync_client as sync_client


def test_retirement_reads_placement_config_once_per_pass(monkeypatch):
    loader = Mock(return_value={"creator_session": "some-placeholder"})
    monkeypatch.setattr(placement, "config", loader)
    with sqlite3.connect(":memory:") as shadow, sqlite3.connect(":memory:") as live:
        shadow.execute("CREATE TABLE task_chat_links(uid TEXT, chat_session TEXT, chat_gateway TEXT)")
        shadow.executemany("INSERT INTO task_chat_links VALUES (?, ?, 'macbook')",
                           [(str(i), f"real-chat-{i}") for i in range(1000)])
        assert sync_client.retire_lost_placeholder_links(live, shadow, "macbook") == 0
        assert loader.call_count == 1
        loader.return_value = {"creator_session": {"hub": "different-placeholder"}}
        assert sync_client.retire_lost_placeholder_links(live, shadow, "macbook") == 0
        assert loader.call_count == 2  # fresh on next pass, not a process-wide stale cache


@pytest.mark.parametrize("cfg,value,expected", [
    ({}, "kanban-placement-macbook", True),
    ({}, "@session:default@hub/kanban-placement-any", True),
    ({"creator_session": "custom"}, "@session:default@hub/custom", True),
    ({"creator_session": {"hub": "custom", "mini": "other"}}, "other", True),
    ({"creator_session": {"hub": "custom"}}, "real-chat", False),
    ({}, "", False),
    ({}, "real-chat", False),
])
def test_snapshot_matches_default_classifier(monkeypatch, cfg, value, expected):
    loader = Mock(return_value=cfg)
    monkeypatch.setattr(placement, "config", loader)
    assert placement.is_placeholder_session(value) is expected
    loader.reset_mock()
    assert placement.is_placeholder_session(value, placement_config=cfg) is expected
    loader.assert_not_called()
