"""A human request cannot turn into a timed authorization or outlive completion."""
import json

import pytest

from plugins.kanban.dashboard import plugin_api


def test_direct_resume_retires_request(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Release", assignee="worker",
                              initial_status="blocked", human_request=REQUEST)
        assert plugin_api._set_status_direct(conn, task, "ready")
        assert db.get_task(conn, task).human_request is None
    finally:
        conn.close()


def test_failure_breaker_never_resurrects_legacy_request(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Release", assignee="worker")
        # Pre-migration rows can carry stale evidence into a new execution.
        conn.execute("UPDATE tasks SET human_request = ? WHERE id = ?",
                     (json.dumps(REQUEST), task))
        conn.commit()
        assert db._record_task_failure(conn, task, "spawn unavailable",
                                       outcome="spawn_failed", force_trip=True)
        assert db.get_task(conn, task).status == "triage"
        assert db.get_task(conn, task).human_request is None
    finally:
        conn.close()

from hermes_cli import kanban_db as db
from hermes_cli.kanban_db_connect import connect

REQUEST = {
    "action": "Approve the staging release or decline it.",
    "why_human": "Release policy requires the owner's decision.",
    "interaction": "approval",
    "scope": "Staging release",
    "evidence": "Release policy requires explicit approval.",
}


def test_explicit_request_cannot_be_silently_timed(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Release", assignee="worker")
        before = [tuple(row) for row in conn.execute("SELECT * FROM task_events")]
        with pytest.raises(ValueError, match="human_request"):
            db.block_task(conn, task, kind="needs_input", human_request=REQUEST,
                          reason="Approval pending SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                          clock_only=True)
        assert db.get_task(conn, task).status == "ready"
        assert [tuple(row) for row in conn.execute("SELECT * FROM task_events")] == before
    finally:
        conn.close()


def test_completion_retires_request(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Release", assignee="worker",
                              initial_status="blocked", human_request=REQUEST)
        assert db.complete_task(conn, task, result="Release cancelled by owner")
        finished = db.get_task(conn, task)
        assert finished.status == "done"
        assert finished.human_request is None
    finally:
        conn.close()


@pytest.mark.parametrize("transition", ["archive", "claim", "review", "schedule", "invalidate", "supervisor"])
def test_legacy_request_retired_by_lifecycle_transition(tmp_path, transition):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Legacy", assignee="worker")
        conn.execute("UPDATE tasks SET human_request = ? WHERE id = ?",
                     (json.dumps(REQUEST), task))
        conn.commit()
        def invalidate():
            parent = db.create_task(conn, title="Parent")
            assert db.complete_task(conn, parent, result="Parent implementation finished")
            db.link_tasks(conn, parent, task)
            assert plugin_api._set_status_direct(conn, parent, "ready")
            return db.get_task(conn, task).status == "todo"

        from hermes_cli.kanban_block_routing import supervisor_stop

        actions = {
            "supervisor": lambda: supervisor_stop(conn, task, "Guard held execution", "respawn_guard_hold"),
            "archive": lambda: db.archive_task(conn, task),
            "claim": lambda: db.claim_task(conn, task) is not None,
            "review": lambda: db.request_review(conn, task),
            "schedule": lambda: db.schedule_task(conn, task, reason="Wait for clock",
                                                  wake_check="2099-01-01T00:00:00Z", clock_only=True,
                                                  awaiting={"kind": "clock", "why": "the clock"}),
            "invalidate": invalidate,
        }
        assert actions[transition]()
        assert db.get_task(conn, task).human_request is None
    finally:
        conn.close()
