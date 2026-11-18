"""Full/lean outcome parity and same-snapshot wait derivation."""
from __future__ import annotations

import json

import pytest

import hermes_state_outcome as outcome
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_session_outcome_tasks as lean
from hermes_cli import kanban_session_wait_rows as wait_rows
from tests.hermes_cli import test_kanban_session_outcome_tasks as helpers

kanban_root = helpers.kanban_root


@pytest.mark.parametrize("status,request_value,kind", [
    ("blocked", helpers.HUMAN_REQUEST, "needs_input"),
    ("blocked", "{broken", "needs_input"),
    ("blocked", {"action": "incomplete"}, "needs_input"),
    ("blocked", helpers.HUMAN_REQUEST, "transient"),
    ("triage", helpers.HUMAN_REQUEST, "needs_input"),
    ("scheduled", None, None),
    ("running", None, None),
    ("review", None, None),
])
@pytest.mark.parametrize("stale", [False, True])
def test_full_and_lean_produce_identical_decisions_and_waits(kanban_root, monkeypatch, status, request_value, kind, stale):
    path = kanban_root / "kanban.db"
    conn = helpers._connect(path)
    helpers._insert_task(conn, "t1", status, "chat", request=request_value, block_kind=kind)
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                 ("t1", "scheduled", json.dumps({"wake_at": 9_999_999_999}), 100.0))
    conn.commit()
    conn.close()
    monkeypatch.setattr(activity, "_remote_cards", lambda *args, **kwargs: [{
        "id": "r1", "key": "remote/default/default/r1", "board": "default", "gateway": "remote",
        "session": "chat", "status": status, "title": "Remote", "human_request": request_value,
        "block_kind": kind, "stale": stale, "wait": {"until": 9_999_999_999, "eta": "later"},
    }])
    actual = outcome._card_items(["chat"])
    monkeypatch.setattr(activity, "session_outcome_tasks", activity.session_tasks)
    expected = outcome._card_items(["chat"])
    assert actual == expected


def test_lean_wait_uses_the_selection_read_transaction(kanban_root, monkeypatch):
    path = kanban_root / "kanban.db"
    writer = helpers._connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    helpers._insert_task(writer, "t1", "scheduled", "chat")
    writer.commit()
    seen = []
    original = wait_rows._local_wait_info

    def raced(conn, found):
        # A concurrent park committed after card selection must not leak into
        # this outcome's wait. The next request must see it.
        seen.append(conn.in_transaction)
        writer.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                       ("t1", "scheduled", json.dumps({"wake_at": 9_999_999_999}), 100.0))
        writer.commit()
        seen.append(conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
        return original(conn, found)

    monkeypatch.setattr(wait_rows, "_local_wait_info", raced)
    try:
        lean._read_lean_session_tasks(path, ["chat"], activity.LISTED_STATUSES)
        assert seen == [True, 0]
    finally:
        writer.close()
