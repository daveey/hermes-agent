"""A relay's red reason names the same owner as the chat task list. [daveey]"""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_chat_links_parity as helpers

env = helpers.env


@pytest.mark.parametrize("released", [0, 1])
@pytest.mark.parametrize("status", ["blocked", "scheduled", "done"])
def test_relay_reason_and_list_share_owner(env, released, status):
    with kbc.connect(env["home"] / "kanban.db") as conn:
        relay = kb.create_task(conn, title="Forwarded relay", assignee="default", session_id="s1")
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (relay,))
        conn.commit()
    path = helpers.replica(env, [("t_remote", status, helpers.HUB, "needs_input")],
                           [("t_remote", "s1", kind, released) for kind in ("created", "shepherd")])
    helpers._ask_on(path, "t_remote")
    n, row = helpers.counted("s1")
    tasks = ksa.session_tasks(["s1"], stale_ok=True)
    assert n == len(tasks)
    blocked = status == "blocked" and not released
    assert row.get("kanban_blocked", 0) == int(blocked)
    if blocked:
        owner = next(t for t in tasks if t["id"] == "t_remote")
        assert owner["status"] == "blocked"
        assert owner["human_request"]["action"]
        assert row["kanban_blocked_on"] == [owner["key"]]
    else:
        assert "kanban_blocked_on" not in row


def test_reason_caps_sorted_unique_blocked_owners():
    from hermes_cli import kanban_activity_identity as identity

    row = {}
    keys = {"s1": {(f"remote/default/t_{i:03}", "blocked", False) for i in range(8)} |
                  {("other/default/t_running", "active", False)}}
    identity.stamp_blocked_owners(row, keys, ["s1", "unrelated"], 3)
    assert row["kanban_blocked_on"] == [f"remote/default/t_{i:03}" for i in range(3)]


def test_reason_uses_frozen_count_snapshot_and_clears_retired_reason(monkeypatch):
    keys = {"s1": {("hub/default/t_remote", "blocked", False)},
            "s2": {("hub/default/t_remote", "blocked", False), ("default/t_local", "blocked", False)}}
    parts = ({}, {"s1": 1, "s2": 2}, {}, {}, {}, {}, {}, {}, keys, {}, {})
    monkeypatch.setattr(ksa, "_annotation_snapshot", lambda: parts)
    row = {"id": "s2", "_lineage_ids": ["s1", "s2"]}
    ksa.annotate_kanban_activity([row])
    assert row["kanban_blocked"] == 2
    assert row["kanban_blocked_on"] == ["default/t_local", "hub/default/t_remote"]
    keys.clear()
    parts[1].clear()
    ksa.annotate_kanban_activity([row])
    assert "kanban_blocked_on" not in row
    row["kanban_blocked_on"] = ["old/owner"]
    monkeypatch.setattr(ksa, "_annotation_snapshot", lambda: None)
    ksa.annotate_kanban_activity([row])
    assert row["kanban_unknown"]
    assert "kanban_blocked_on" not in row


def test_failed_read_drops_needs_you(monkeypatch):
    """A failed annotation read is unknown, so a carried ask must not stay on the row."""
    monkeypatch.setattr(ksa, "_annotation_snapshot", lambda: None)
    row = {
        "id": "s1",
        "needs_you": [{"kind": "card", "ref": "default/t1", "question": ""}],
        "kanban_blocked": 1,
        "kanban_blocked_on": ["default/t1"],
    }
    ksa.annotate_kanban_activity([row])
    assert row["kanban_unknown"]
    assert "needs_you" not in row
    assert "kanban_blocked" not in row
