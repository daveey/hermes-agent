"""Continuation budgets are agent recovery work, not a human decision."""
import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as activity


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.db"
    kb.init_db(path)
    conn = kbc.connect(path)
    yield conn, path
    conn.close()


def exhausted(conn):
    tid = kb.create_task(conn, title="Long build", assignee="default", session_id="owner")
    assert kb.claim_task(conn, tid)
    assert kc.continue_task(conn, tid, checkpoint="Stage one", max_continuations=1)["ok"]
    assert kb.claim_task(conn, tid)
    refused = kc.continue_task(conn, tid, checkpoint="Stage two", max_continuations=1)
    assert not refused["ok"]
    return tid, refused


@pytest.mark.parametrize("kind,reason", [
    ("needs_input", "Reset the continuation budget or split the work."),
    ("transient", "continuation_exhausted: split the work or reset the continuation budget"),
    ("needs_input", "1) Review the preserved implementation/checkpoint NOTES.md for this card; "
     "the dispatcher refused continuation after its 12-run limit. "
     "2) Reset/requeue the continuation budget or route the remaining integration as bounded specialist work, "
     "then unblock this card. Latest unshipped commitabc123 passes20 tests; live acceptance still remains. "
     "This is not delivered; keep the originating tracker unfinished."),
])
def test_budget_only_human_block_routes_to_agent_triage(board, kind, reason):
    conn, path = board
    tid, refused = exhausted(conn)
    assert kb.block_task(conn, tid, kind=kind, reason=reason)
    task = kb.get_task(conn, tid)
    assert (task.status, task.block_kind) == ("triage", "transient")
    assert refused["reason_code"] == "continuation_exhausted"
    assert refused["block_kind"] == "transient"
    assert activity._read_counts(path, activity.BLOCKED_STATUSES) == {}
    assert activity._read_counts(path) == {"owner": 1}
    assert activity._replica_bucket({"status": task.status, "block_kind": task.block_kind}) == "active"
    events = kb.list_events(conn, tid)
    assert any(e.payload.get("rekind_reason") == "continuation_exhausted" for e in events)


@pytest.mark.parametrize("kind,reason,human", [
    ("needs_input", "Which production region should we deploy to?", True),
    ("needs_input", "Reset the continuation budget. Which production region should we deploy to?", True),
    ("needs_input", "Reset the continuation budget. Latest unshipped commit abc123 passes 20 tests; "
     "please approve production deployment, which still remains.", True),
    ("capability", "Restore the deployment credentials, then unblock this card.", True),
    ("time", "Waiting for the release window.", False),
])
def test_real_human_questions_and_clock_waits_keep_their_kind(board, kind, reason, human):
    conn, path = board
    tid, _ = exhausted(conn)
    request = {
        "action": reason,
        "why_human": "Only the release owner can select the target or restore access.",
        "interaction": "reply" if kind == "needs_input" else "user_terminal",
        "scope": "This production deployment only",
        "evidence": "Release policy reserves target selection and credential recovery to the owner.",
    } if human else None
    assert kb.block_task(conn, tid, kind=kind, reason=reason, clock_only=kind == "time",
                         human_request=request,
                         awaiting={"kind": "clock", "why": "the release window"} if kind == "time" else None)
    task = kb.get_task(conn, tid)
    assert bool(activity._read_counts(path, activity.BLOCKED_STATUSES)) == human
    replica = {"status": task.status, "block_kind": task.block_kind,
               "human_request": task.human_request}
    assert (activity._replica_bucket(replica) == "blocked") == human


def test_budget_words_without_refusal_are_not_reclassified(board):
    conn, _ = board
    tid = kb.create_task(conn, title="Budget decision", assignee="default")
    assert kb.block_task(conn, tid, kind="needs_input", reason="Reset the continuation budget or split the work.")
    assert kb.get_task(conn, tid).block_kind == "needs_input"


def test_refusal_does_not_leak_to_a_new_run_or_accept_a_stale_worker(board):
    conn, _ = board
    tid, _ = exhausted(conn)
    run_id = kb.get_task(conn, tid).current_run_id
    reason = "Reset the continuation budget or split the work."
    assert not kb.block_task(conn, tid, kind="needs_input", reason=reason, expected_run_id=run_id + 1)
    assert kb.get_task(conn, tid).status == "running"
    assert kb.block_task(conn, tid, kind="transient", reason="Retry network")
    assert kb.unblock_task(conn, tid)
    assert kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, kind="needs_input", reason=reason)
    assert kb.get_task(conn, tid).block_kind == "needs_input"
