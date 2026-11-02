"""Finished exact-head reviews are continuation evidence, not merge approval."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_dependency_review as review
from hermes_cli import kanban_db_dispatch_guard_notice as notice
from hermes_cli import kanban_dependency_review_notice as review_notice
from tests.hermes_cli import test_kanban_dependency_review as helpers

board = helpers.board
PR = "https://github.com/daveey/hermes/pull/682"
SHA = "80a1696031723d5f0ece4c3f5adda458729a560e"


def incident(board, monkeypatch, *, finish=True, metadata=None, summary=None):
    monkeypatch.setattr(helpers, "PR", PR)
    task, parents = helpers.parked(board)
    data = {"reviewed_pr": PR, "reviewed_sha": SHA, "base_sha": "b" * 40,
            "tests_run": 274, "live_probe": {"ok": True}, "findings": [{}] * 6,
            "parent_comment_id": 1, "worker_session_id": "review-session"}
    if metadata is not None:
        data = metadata
    if finish:
        helpers._finish(board, parents, data, summary or "Reviewed PR #682. No blocking defects; one medium finding.")
    return task, parents


def test_incident_resumes_exactly_once(board, monkeypatch):
    task, parents = incident(board, monkeypatch, finish=False)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    helpers._finish(board, parents, {
        "reviewed_pr": PR, "reviewed_sha": SHA, "base_sha": "b" * 40,
        "tests_run": 274, "live_probe": {"ok": True}, "findings": [{}] * 6,
        "parent_comment_id": 1, "worker_session_id": "review-session",
    }, "Reviewed PR #682. No blocking defects; one medium finding.")
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("extra", [{"approved": True}, {"approved": False}, {"verdict": "observations"}, {"verdict": None}])
@pytest.mark.parametrize("key", ["reviewed_sha", "head"])
def test_unrecognised_verdict_is_repair_not_approval(board, monkeypatch, extra, key):
    task, _ = incident(board, monkeypatch, metadata={key: SHA, **extra})
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("named", [PR, "https://github.com/daveey/hermes/pull/999"])
def test_reviewed_pr_binds_without_summary_number(board, monkeypatch, named):
    task, _ = incident(board, monkeypatch, metadata={"reviewed_sha": SHA, "reviewed_pr": named},
                       summary="No blocking defects")
    assert dispatch.check_respawn_guard(board, task) == (None if named == PR else "active_pr")


@pytest.mark.parametrize("mode", ["short", "missing", "before", "own", "pending", "outcome", "pr", "summary", "uid"])
def test_other_refusals_survive(board, monkeypatch, mode):
    data = {"reviewed_sha": SHA}
    if mode == "short":
        data["reviewed_sha"] = SHA[:8]
    if mode == "missing":
        data = {}
    if mode == "pr":
        data["pr"] = PR + "1"
    task, parents = incident(board, monkeypatch, metadata=data,
                             summary="Reviewed PR #999" if mode == "summary" else None,
                             finish=mode != "pending")
    event = board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='dependency_wait'", (task,)).fetchone()
    if mode in {"before", "outcome", "uid"}:
        with kb.write_txn(board):
            if mode == "before":
                board.execute("UPDATE task_runs SET ended_at=? WHERE task_id IN (?,?)", (event["created_at"] - 1, *parents))
            elif mode == "outcome":
                board.execute("UPDATE task_runs SET outcome='crashed' WHERE task_id IN (?,?)", parents)
            else:
                payload = json.loads(event["payload"])
                payload["review_checkpoint"]["run_uid"] = "wrong-uid"
                board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
    if mode == "own":
        parent_run = board.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (parents[0],)).fetchone()[0]
        assert review._completed_review_run(board, parents[0], event["created_at"], parent_run) is None
        return
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("finished", [True, False])
def test_finished_unreadable_review_notices_immediately(board, monkeypatch, finished):
    task, parents = incident(board, monkeypatch, finish=finished, metadata={"reviewed_sha": "short"})
    monkeypatch.setattr(notice, "_pr_failing_checks", lambda _: "")
    fixes = []
    monkeypatch.setattr(notice, "_file_fix_card", lambda *args: fixes.append(args))
    now = notice.time.time()
    monkeypatch.setattr(notice.time, "time", lambda: now)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    notice.record_guard_hold(board, task, "active_pr")
    assert bool(fixes) is finished
    if finished:
        body = fixes[0][-1]
        assert parents[0] in body
        assert "exact_head" in body
        assert "for less than 1 min" in body
        def unexpected_scan(*args):
            pytest.fail("already-notified hold rescanned review evidence")
        monkeypatch.setattr(review_notice, "finished_review_hold", unexpected_scan)
        notice.record_guard_hold(board, task, "active_pr")
        assert len(fixes) == 1
    else:
        start = notice.guard_hold_started_at(board, task)
        monkeypatch.setattr(notice.time, "time", lambda: start + 1799)
        assert not notice.notify_guard_hold(board, task, "active_pr")
        monkeypatch.setattr(notice.time, "time", lambda: start + 1800)
        assert notice.notify_guard_hold(board, task, "active_pr")
