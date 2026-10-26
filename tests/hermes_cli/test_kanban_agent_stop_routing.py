"""Dispatcher failures must reach agent triage, not the human inbox."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.db"
    kb.init_db(path)
    conn = kbc.connect(path)
    yield conn, path
    conn.close()


def assert_agent(conn, path, tid):
    task = kb.get_task(conn, tid)
    assert (task.status, task.block_kind) == ("triage", "transient")
    assert activity._read_counts(path, activity.BLOCKED_STATUSES) == {}
    assert activity._replica_bucket({"status": task.status, "block_kind": task.block_kind}) == "active"


def test_gave_up_is_agent_triage(board):
    conn, path = board
    tid = kb.create_task(conn, title="Worker failure", assignee="default", session_id="owner")
    assert kb.claim_task(conn, tid)
    assert dispatch._record_task_failure(conn, tid, "Worker crashed", outcome="crashed",
                                         failure_limit=1, release_claim=True, end_run=True)
    assert_agent(conn, path, tid)


@pytest.mark.parametrize("reason", ["active_pr", "blocker_auth"])
def test_guard_hold_is_agent_triage(board, monkeypatch, reason):
    conn, path = board
    tid = kb.create_task(conn, title="Guard hold", assignee="default", session_id="owner")
    monkeypatch.setattr(dispatch, "_guard_hold_started_at", lambda *args: 1)
    assert dispatch._block_if_guard_hold_capped(conn, tid, reason)
    assert kb.recompute_ready(conn, failure_limit=100) == 0
    assert_agent(conn, path, tid)


def test_repeated_agent_block_stays_agent_but_approval_stays_human(board):
    conn, path = board
    for kind in ("transient", "needs_input"):
        tid = kb.create_task(conn, title="Repeated stop", assignee="default", session_id="owner")
        reason = "Retry network" if kind == "transient" else "Approve production deployment?"
        request = {
            "action": reason,
            "why_human": "Release policy requires the owner's approval.",
            "interaction": "approval",
            "scope": "This production deployment only",
            "evidence": "Production deployment is subject to the release approval policy.",
        } if kind == "needs_input" else None
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
            assert kb.block_task(conn, tid, kind=kind, reason=reason, human_request=request)
            if kb.get_task(conn, tid).status != "triage":
                assert kb.unblock_task(conn, tid)
        if kind == "transient":
            assert kb.recompute_ready(conn, failure_limit=100) == 0
            assert_agent(conn, path, tid)
        else:
            assert kb.get_task(conn, tid).block_kind == "needs_input"
            assert activity._read_counts(path, activity.BLOCKED_STATUSES) == {"owner": 1}


def test_impossible_dependency_loop_is_not_a_human_question(board):
    conn, path = board
    tid = kb.create_task(conn, title="Missing dependency", assignee="default", session_id="owner")
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
        assert kb.block_task(conn, tid, kind="dependency", reason="Waiting on missing prerequisite",
                             blocker_id="test-shared-wall")
        if kb.get_task(conn, tid).status != "triage":
            assert kb.unblock_task(conn, tid)
    assert_agent(conn, path, tid)
