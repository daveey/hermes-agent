"""Unconfirmed immutable requests retire automation, not execution holds."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli import kanban_spend_review_request as journal
from hermes_cli import kanban_spend_stop_watch as watch
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_spend_caused_review as helpers
from tests.hermes_cli.test_kanban_spend_review_reconcile import started

kanban_home = helpers.kanban_home
board = helpers.board


@pytest.mark.parametrize("failure", ["before_submit", "uncertain"])
@pytest.mark.parametrize("fence", ["done", "archived", "human", "safety"])
def test_unconfirmed_request_has_bounded_administrative_handoff(
        kanban_home, spend_reviewer, board, monkeypatch, failure, fence):
    hub, calls = board
    if failure == "before_submit":
        def push_down(*args, **kwargs):
            raise client.HubError(503, "push unavailable")
        monkeypatch.setattr(client, "push", push_down)
    else:
        real_ops = hub.ops
        def uncertain(board_id, ops):
            real_ops(board_id, ops)
            raise OSError("reply lost after canonical application")
        hub.ops = uncertain
    monkeypatch.setattr(reviewer, "judge", lambda ctx: {
        "decision": "release", "reason": "frozen", "remaining_quote": helpers.QUOTE})
    with kbc.connect_closing() as conn:
        tid, ep = started(conn)
        assert reviewer.review(conn, tid, ep) == "pending"
        original = journal.get(conn, tid, ep)
        submitted = list(calls)
        if fence == "human":
            kb.add_comment(conn, tid, "operator", "Wait for me", writer_kind="human")
        else:
            with kb.write_txn(conn):
                if fence == "safety":
                    kb._append_event(conn, tid, "decision_hold_restricted", {"episode": ep, "cause": "safety_refusal"})
                else:
                    conn.execute("UPDATE tasks SET status=? WHERE id=?", (fence, tid))
        reads = []
        def absent(*args):
            reads.append(args)
            return {"state": "absent"}
        hub.row_evidence = absent
        monkeypatch.setattr(reviewer, "judge", lambda ctx: pytest.fail("second judge"))
        reviewer.start_due(conn, now=10**10)
        reviewer.wait_for_reviews()
        # One absence is not cancellation, settlement, or authority to release.
        assert reviewer.state(conn, tid, ep) == reviewer.STARTED
        assert journal.get(conn, tid, ep) == original
    # Restarted connection must retain the same bounded reconciliation history.
    with kbc.connect_closing() as conn:
        for i in range(1, 20):
            reviewer.start_due(conn, now=10**10 + i * (reviewer.LEASE_SECONDS + 1))
            reviewer.wait_for_reviews()
        assert reviewer.state(conn, tid, ep) == reviewer.ESCALATED
        assert not reviewer.holds(conn, tid, ep)
        assert journal.get(conn, tid, ep) == original
        assert calls == submitted
        assert len(reads) == journal.MAX_RECONCILE_ATTEMPTS - 1
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
                            (tid, reviewer.STARTED)).fetchone()[0] == journal.MAX_RECONCILE_ATTEMPTS
        assert reviewer.review(conn, tid, ep) == "moot"
        assert len(reads) == journal.MAX_RECONCILE_ATTEMPTS - 1
        assert not any(t == tid and e == ep for t, e, _ in reviewer._due(conn, 10**12, set()))
        assert kb.get_task(conn, tid).status == (fence if fence in {"done", "archived"} else "triage")
        events = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, reviewer.ESCALATED))]
        assert len(events) == 1
        notice = kb.get_task(conn, events[0]["task_id"])
        assert notice is not None and notice.status not in {"done", "archived"}
        assert original["wire"]["event_uid"] in notice.body
        assert "not cancellation" in notice.body
        assert "Do not resubmit" in notice.body
        if fence in {"human", "safety"}:
            assert tid in [c["id"] for c in watch.stale_stops(conn, now=10**12)]
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                                "('spend_review_granted','spend_review_settled','promoted_manual')", (tid,)).fetchone()
