"""One open-human-ask predicate: the badge count, the outcome's decisions and ``needs_you`` agree for every card.

[daveey] The sidebar badge (``kanban_blocked``), the stored outcome (``_card_items`` decisions) and the Desktop stripe
once worked out "is a human asked?" separately. This pins that they agree for every combination of card status x
block_kind x human_request, on the local read, the replica bucket and the row annotation.
"""

from __future__ import annotations

import json

import pytest

import hermes_state_outcome as outcome
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_open_asks import card_is_open_human_ask, open_human_asks
from tests.hermes_cli import test_kanban_session_outcome_tasks as helpers

kanban_root = helpers.kanban_root

STATUSES = ("blocked", "triage", "todo", "ready", "running", "scheduled", "review")
KINDS = (None, "needs_input", "capability", "transient", "dependency")
REQUESTS = {
    "none": None,
    "valid": helpers.HUMAN_REQUEST,
    "valid-json-text": json.dumps(helpers.HUMAN_REQUEST),
    "incomplete": {"action": "incomplete"},
    "broken-text": "{broken",
}


def _board(root, rows):
    conn = helpers._connect(root / "kanban.db")
    conn.execute("ALTER TABLE tasks ADD COLUMN completed_at REAL")  # the done-evidence read needs it
    for tid, status, request, kind in rows:
        helpers._insert_task(conn, tid, status, "chat", request=request, block_kind=kind)
    conn.commit()
    conn.close()
    ksa._reset_cache_for_tests()


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("request_name", sorted(REQUESTS))
def test_badge_outcome_and_needs_you_agree(kanban_root, status, kind, request_name):
    request = REQUESTS[request_name]
    expected = card_is_open_human_ask(status, kind, request)
    _board(kanban_root, [("t1", status, request, kind)])
    assert (ksa.blocked_task_counts().get("chat", 0) == 1) is expected  # the badge's own read
    assert (ksa._replica_bucket({"status": status, "block_kind": kind, "human_request": request}) == "blocked") is expected

    rows = [{"id": "chat"}]
    ksa.annotate_kanban_activity(rows)
    assert rows[0].get("kanban_blocked", 0) == int(expected)
    assert len(rows[0].get("needs_you", [])) == int(expected)

    decisions, _waits = outcome._card_items(["chat"])
    assert len(decisions) == int(expected)
    if expected:
        assert rows[0]["needs_you"][0]["kind"] == "card"
        assert rows[0]["needs_you"][0]["ref"].endswith("t1")
        assert decisions[0]["card_id"] == "t1"


def test_a_validated_ask_needs_a_blocked_status():
    """A stale ``human_request`` on a card that moved on is not an ask."""
    for status in ("todo", "ready", "running", "scheduled", "review", "done", "archived"):
        assert not card_is_open_human_ask(status, "needs_input", helpers.HUMAN_REQUEST)
    for status in ("blocked", "triage"):
        assert card_is_open_human_ask(status, "needs_input", helpers.HUMAN_REQUEST)


def test_needs_you_never_undercounts_the_badge():
    assert open_human_asks([], 0) == []
    assert [e["ref"] for e in open_human_asks(["default/t_b", "default/t_a", "default/t_a"], 2)] == [
        "default/t_a", "default/t_b"]
    padded = open_human_asks(["default/t_a"], 3)  # a replica row that carried no key
    assert len(padded) == 3 and padded[0]["ref"] == "default/t_a" and padded[2]["ref"] == ""


def test_row_without_asks_carries_no_needs_you_and_no_blocked_count(kanban_root):
    _board(kanban_root, [("t1", "running", None, None)])
    rows = [{"id": "chat"}]
    ksa.annotate_kanban_activity(rows)
    assert "needs_you" not in rows[0]
    assert "kanban_blocked" not in rows[0]
