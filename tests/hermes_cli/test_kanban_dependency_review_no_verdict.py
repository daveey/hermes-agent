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


def incident(board, monkeypatch, *, finish=True, metadata=None, summary=None, park_delay=1):
    monkeypatch.setattr(helpers, "PR", PR)
    task, parents = helpers.parked(board, park_delay=park_delay)
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


@pytest.mark.parametrize("verdict", [None, "approve", "approved"])
@pytest.mark.parametrize("named", [PR, "https://github.com/daveey/hermes/pull/999"])
def test_reviewed_pr_binds_without_summary_number(board, monkeypatch, named, verdict):
    task, _ = incident(board, monkeypatch, metadata={"reviewed_sha": SHA, "reviewed_pr": named, "verdict": verdict},
                       summary="No blocking defects")
    assert dispatch.check_respawn_guard(board, task) == (None if named == PR else "active_pr")


@pytest.mark.parametrize("park_delay", [0, 1])
@pytest.mark.parametrize("mode", ["short", "missing", "before", "own", "pending", "outcome", "pr", "summary", "uid"])
def test_other_refusals_survive(board, monkeypatch, mode, park_delay):
    data = {"reviewed_sha": SHA}
    if mode == "short":
        data["reviewed_sha"] = SHA[:8]
    if mode == "missing":
        data = {}
    if mode == "pr":
        data["pr"] = PR + "1"
    task, parents = incident(board, monkeypatch, metadata=data,
                             summary="Reviewed PR #999" if mode == "summary" else None,
                             finish=mode != "pending", park_delay=park_delay)
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


@pytest.mark.parametrize("metadata,summary,expected", [
    # A review whose metadata was lost still names the checkpointed PR: review rules own it.
    ({}, "Reviewed PR #682. No blocking defects; one medium finding.", "active_pr"),
    ({}, "Looked at " + PR + " and found nothing blocking", "active_pr"),
    ({"pr": PR}, "Review complete", "active_pr"),
    # A dispatcher worker's completion adds worker_session_id; with only that and a PR reference left, still a review.
    ({"worker_session_id": "review-session"}, "Reviewed PR #682. No blocking defects.", "active_pr"),
    ({"reviewed_pr": PR, "worker_session_id": "review-session"}, "Review complete", "active_pr"),
    # A finished ordering prerequisite that names no checkpointed PR keeps its one continuation.
    ({}, "ordering work finished", None),
    ({"head": SHA}, "gate finished", None),
    ({}, "Unrelated PR #999 merged", None),
    # So does one that reports its own work and names the child's PR only as context
    # (shapes from hub results: CI premise, CI gate, implementation, landed sibling fix, CI triage).
    ({"pr": PR, "head": SHA[:7], "merge_sha": "b" * 40, "ci_run": 40761355194},
     "PR682 squash-merged; CI run 40761355194 attempt 2 was green.", None),
    ({"pr": PR, "head": SHA[:7], "ci_run": 1, "decision": "wait"},
     "PR #682 head is still 80a1696 and not mergeable yet.", None),
    ({"core_pr": PR, "core_sha": "c" * 40, "core_tests": "37 passed"},
     "Implemented and published core PR #682 with exact-head tests passing.", None),
    ({"pr": PR, "merge_commit": "d" * 40, "green_main_run": 1}, "PR #682 merged; main CI is green.", None),
    ({"reviewed_pr": PR, "resolution": "already repaired", "failure_run": 1},
     "Identified the shard failure in " + PR + "; already repaired on main.", None),
    # A review that keeps its outcome under other keys (4 hub reviews use review_outcome and
    # reviewer_checks) is not recognised and takes the continuation, as it did before #728.
    ({"reviewed_pr": PR, "review_outcome": "approved", "reviewer_checks": ["exact-head tests"],
      "landed_sha": "e" * 40, "worker_session_id": "review-session"},
     "Reviewed and approved PR #682; landed as eeeeeee.", None),
])
def test_ordering_handoff_does_not_absorb_unreadable_review(board, monkeypatch, metadata, summary, expected):
    """Park a second after the PR comment (production order): the ordering handoff
    releases only parents that are not reviews of the checkpointed PR."""
    task, _ = incident(board, monkeypatch, metadata=metadata, summary=summary, park_delay=1)
    assert dispatch.check_respawn_guard(board, task) == expected


@pytest.mark.parametrize("where", ["summary", "result"])
def test_lost_review_citing_its_report_file_stays_held(board, monkeypatch, where):
    """Completion adds an artifacts list when the summary or the result names a scratch-workspace
    file, so a review whose verdict was lost but which cites its report file must still be held."""
    from hermes_cli import kanban_db_workspace as kbw
    task, parents = incident(board, monkeypatch, finish=False)
    stored_keys = []
    for parent in parents:
        kb.claim_task(board, parent)
        row = kb.get_task(board, parent)
        assert row is not None
        workspace = kbw.resolve_workspace(row)
        kbw.set_workspace_path(board, parent, workspace)
        report = workspace / "review.md"
        report.write_text("No blocking defects.\n")
        if where == "summary":
            summary, result = f"Reviewed PR #682. No blocking defects; report: {report}", None
        else:  # the PR in the summary, the report path only in the result
            summary, result = "Reviewed PR #682. No blocking defects.", f"Report: {report}"
        assert kb.complete_task(board, parent, summary=summary, result=result,
                                expected_run_id=row.current_run_id, metadata={})
        stored = kb._json_dict(board.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (parent,)).fetchone()[0])
        assert "artifacts" in stored, stored  # added by completion: the worker passed no metadata
        stored_keys.append(sorted(stored))
    kb.recompute_ready(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr", stored_keys


def test_lost_review_completed_by_the_worker_tool_stays_held(board, monkeypatch):
    """A worker's kanban_complete stamps worker_session_id on its own card and folds artifacts= into
    metadata; a review whose verdict was lost and that names the PR in its summary is still held."""
    from hermes_cli import kanban_db_workspace as kbw
    from tools import kanban_tools as kt
    task, parents = incident(board, monkeypatch, finish=False)
    for parent in parents:
        kb.claim_task(board, parent)
        row = kb.get_task(board, parent)
        assert row is not None
        workspace = kbw.resolve_workspace(row)
        kbw.set_workspace_path(board, parent, workspace)
        report = workspace / "review.md"
        report.write_text("No blocking defects.\n")
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(row.current_run_id))
        monkeypatch.setenv("HERMES_SESSION_ID", "review-session")
        out = json.loads(kt._handle_complete({"summary": "Reviewed PR #682. No blocking defects.",
                                              "artifacts": [str(report)]}))
        assert out["ok"] is True, out
        stored = kb._json_dict(board.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (parent,)).fetchone()[0])
        assert sorted(stored) == ["artifact_receipt", "artifacts", "worker_session_id"], stored
    kb.recompute_ready(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("tool", ["continue", "request_review"])
def test_continue_and_request_review_stamp_worker_session_id(board, monkeypatch, tool):
    """The _LOST_REVIEW_KEYS comment says kanban_continue and kanban_request_review stamp worker_session_id
    like kanban_complete does; pin each of those entry paths on the run the tool ends."""
    from tools import kanban_tools as kt
    _task, parents = incident(board, monkeypatch, finish=False)
    parent = parents[0]
    kb.claim_task(board, parent)
    row = kb.get_task(board, parent)
    assert row is not None and row.current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(row.current_run_id))
    session = f"{tool}-worker-session"  # not incident()'s "review-session": only the tool can have written it
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    if tool == "continue":
        out = json.loads(kt._handle_continue({"checkpoint": "Reviewed half of PR #682; next: the tests."}))
    else:
        out = json.loads(kt._handle_request_review({"summary": "Reviewed PR #682. No blocking defects."}))
    assert out["ok"] is True, out
    stored = kb._json_dict(board.execute(
        "SELECT metadata FROM task_runs WHERE id = ?", (row.current_run_id,)).fetchone()[0])
    assert stored.get("worker_session_id") == session, stored


@pytest.mark.parametrize("tool", ["complete", "continue", "request_review"])
def test_worker_tools_stamp_no_session_id_for_another_caller(board, monkeypatch, tool):
    """The stamp is the worker's own: an orchestrator or CLI caller (no HERMES_KANBAN_TASK) that names the card
    keeps its own HERMES_SESSION_ID off the run it ends, or the dashboard would show it as the worker's session."""
    from tools import kanban_tools as kt
    _task, parents = incident(board, monkeypatch, finish=False)
    parent = parents[0]
    kb.claim_task(board, parent)
    row = kb.get_task(board, parent)
    assert row is not None and row.current_run_id
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "orchestrator-session")
    handle, args, outcome = {
        "complete": (kt._handle_complete, {"summary": "Reviewed PR #682. No blocking defects."}, "completed"),
        "continue": (kt._handle_continue, {"checkpoint": "Reviewed half of PR #682; next: the tests."}, "continued"),
        "request_review": (kt._handle_request_review, {"summary": "Reviewed PR #682. No blocking defects."},
                           "review_requested"),
    }[tool]
    out = json.loads(handle({"task_id": parent, **args}))
    assert out["ok"] is True, out
    ended, metadata = board.execute(
        "SELECT outcome, metadata FROM task_runs WHERE id = ?", (row.current_run_id,)).fetchone()
    assert ended == outcome, ended
    assert "worker_session_id" not in kb._json_dict(metadata), metadata


@pytest.mark.parametrize("captured", [None, []])
def test_lost_review_needs_a_checkpointed_pr_to_name(captured):
    """checkpoint() never stores an empty capture, so one here comes from a checkpoint written without "prs"
    (older code or another gateway). With nothing to name, the pre-#728 rule applies: only verdict keys or a
    VERDICT: line mark a review."""
    assert review.is_review_result({}, "Reviewed PR #682. No blocking defects.", captured) is False
    assert review.is_review_result({"pr": PR, "worker_session_id": "s"}, "Review complete", captured) is False
    assert review.is_review_result({"reviewed_sha": SHA}, "", captured) is True
    assert review.is_review_result({}, "VERDICT: REQUEST_CHANGES", captured) is True
