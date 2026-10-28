"""Actionable human asks are evidence, never inferred permission."""
import pytest

from hermes_cli import kanban_db as db
from hermes_cli.kanban_db_connect import connect


REQUEST = {
    "action": "Approve the staging deployment or decline it.",
    "why_human": "The deployment policy requires the release owner's decision.",
    "interaction": "approval",
    "scope": "Staging deployment only",
    "evidence": "Release policy requires explicit approval for staging.",
}


def test_initial_and_reblocked_requests_are_explicit_and_do_not_execute(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Deploy", assignee="worker", initial_status="blocked",
                              human_request=REQUEST)
        assert db.get_task(conn, task).human_request == REQUEST
        assert db.get_task(conn, task).status == "blocked"
        assert db.unblock_task(conn, task)
        assert db.get_task(conn, task).human_request is None
        assert db.block_task(conn, task, kind="capability", reason="sudo: a password is required")
        assert db.get_task(conn, task).human_request is None
        assert db.get_task(conn, task).status == "blocked"
    finally:
        conn.close()


def test_invalid_request_rejected_before_any_write(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        for field in REQUEST:
            bad = {**REQUEST, field: " "}
            with pytest.raises(ValueError, match="human_request"):
                db.create_task(conn, title="Deploy", initial_status="blocked", human_request=bad)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        task = db.create_task(conn, title="Deploy", assignee="worker")
        with pytest.raises(ValueError, match="human_request"):
            db.block_task(conn, task, kind="transient", human_request=REQUEST)
        assert db.get_task(conn, task).status == "ready"
    finally:
        conn.close()


def test_initial_blocked_with_request_is_classified_needs_input(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Deploy", assignee="worker", initial_status="blocked",
                              human_request=REQUEST)
        row = db.get_task(conn, task)
        assert row.block_kind == "needs_input"
        assert row.human_request == REQUEST
        blocked = [e for e in db.list_events(conn, task) if e.kind == "blocked"]
        assert len(blocked) == 1
        assert blocked[0].payload["kind"] == "needs_input"
        assert blocked[0].payload["reason"] == "initial_status"
    finally:
        conn.close()


def test_initial_blocked_without_request_keeps_kind_unset(tmp_path):
    conn = connect(tmp_path / "board.db")
    try:
        task = db.create_task(conn, title="Swarm root tracker", assignee="worker",
                              initial_status="blocked", lint_blocked=False)
        row = db.get_task(conn, task)
        assert row.status == "blocked"
        assert row.block_kind is None
        assert row.human_request is None
        blocked = [e for e in db.list_events(conn, task) if e.kind == "blocked"]
        assert len(blocked) == 1
        assert "kind" not in blocked[0].payload
    finally:
        conn.close()
