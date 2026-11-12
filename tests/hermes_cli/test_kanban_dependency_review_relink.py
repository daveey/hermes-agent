"""A re-review linked after the owner's checkpoint releases ``active_pr`` once at the current head.

Production shape (t_01a1cde626be2ada, PR #1223): the owner parked awaiting review A, A
requested changes, and an orchestrator then linked re-review B with ``kanban_link``. No
owner park ever named B, so the parent set no longer matched the checkpoint and the owner
stayed held after B approved the PR's current head.
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_dependency_review_relink as relink
from hermes_cli.kanban_dependency_review import resumes_review
from tests.hermes_cli import test_kanban_dependency_review as helpers
from tests.hermes_cli.kanban_guard_helpers import worker_comment

board = helpers.board
PR, HEAD = helpers.PR, helpers.HEAD
OLD = "806ff390d290689d5f6b947bad8378dde49fc994"
_REAL_HEAD = relink._gh_pr_head


@pytest.fixture(autouse=True)
def github_head(monkeypatch):
    heads = {PR: HEAD}
    monkeypatch.setattr(relink, "_gh_pr_head", lambda url: heads.get(url))
    return heads


def parked(conn, *, link_b_before_park=False, prs=(PR,)):
    """Owner publishes ``prs``, parks on review A 10 s ago; returns (task, A, B or None)."""
    parked_at = int(time.time()) - 10
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(time, "time", lambda: parked_at - 1)
        task = kb.create_task(conn, title="implementation", assignee="dev")
        kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        worker_comment(conn, task, "dev", " ".join(prs))
        clock.setattr(time, "time", lambda: parked_at)
        review_a = kb.create_task(conn, title="review A", assignee="reviewer")
        kb.link_tasks(conn, review_a, task, expected_child_run_id=run)
        review_b = None
        if link_b_before_park:
            review_b = kb.create_task(conn, title="review B", assignee="reviewer")
            kb.link_tasks(conn, review_b, task, expected_child_run_id=run)
        assert kb.block_task(conn, task, kind="dependency", reason="Independent review", expected_run_id=run)
    return task, review_a, review_b


def finish(conn, review, metadata):
    kb.claim_task(conn, review)
    run = kb.get_task(conn, review).current_run_id
    assert kb.complete_task(conn, review, summary="Review complete", expected_run_id=run, metadata=metadata)
    kb.recompute_ready(conn)


def changes(sha=OLD, pr=PR):
    return {"verdict": "request_changes", "reviewed_pr": pr, "reviewed_sha": sha}


def approve(sha=HEAD, pr=PR):
    return {"verdict": "approve", "pr": pr, "reviewed_sha": sha}


def relinked(conn, metadata_b=None, *, finish_b=True):
    """A requests changes, the orchestrator links B, B finishes with ``metadata_b``."""
    task, review_a, _ = parked(conn)
    finish(conn, review_a, changes())
    review_b = kb.create_task(conn, title="review B", assignee="reviewer")
    kb.link_tasks(conn, review_b, task)  # orchestrator link: no owner park names B
    if finish_b:
        finish(conn, review_b, approve() if metadata_b is None else metadata_b)
    return task, review_b


def test_linked_rereview_approving_current_head_releases_once(board):
    task, _ = relinked(board)
    assert kb.get_task(board, task).status == "ready"
    assert dispatch.check_respawn_guard(board, task) is None
    # One continuation: the next claim consumes it, and a crash re-arms active_pr.
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"
    assert kb.reclaim_task(board, task, reason="Worker died", signal_fn=lambda *_: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_linked_rereview_requesting_changes_at_current_head_releases(board):
    task, _ = relinked(board, changes(sha=HEAD))
    assert dispatch.check_respawn_guard(board, task) is None


@pytest.mark.parametrize("metadata", [
    approve(pr=PR + "1"),                                 # reviews a different PR
    approve(sha=OLD),                                     # not the PR's current head
    {"verdict": "approve", "reviewed_sha": HEAD},         # names no PR: no checkpoint binding
    {"verdict": "approve", "pr": PR, "head": HEAD},       # head key, not reviewed_sha
    {"verdict": "looks good", "pr": PR, "reviewed_sha": HEAD},  # neither approve nor changes
], ids=["other_pr", "stale_head", "no_pr", "head_key", "no_verdict"])
def test_linked_rereview_without_current_head_evidence_stays_held(board, metadata):
    task, _ = relinked(board, metadata)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_linked_rereview_still_running_stays_held(board):
    task, review_b = relinked(board, finish_b=False)
    kb.claim_task(board, review_b)
    assert kb.get_task(board, review_b).status == "running"
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_unknown_github_head_stays_held(board, github_head):
    github_head.clear()  # gh failed: fail closed
    task, _ = relinked(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_parent_linked_before_the_park_but_missing_from_checkpoint_stays_held(board):
    task, review_a, review_b = parked(board, link_b_before_park=True)
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? "
                        "AND kind = 'dependency_wait' ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    payload = json.loads(row["payload"])
    checkpoint = payload["review_checkpoint"]
    checkpoint["parents"] = checkpoint["awaited"] = [review_a]
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    finish(board, review_a, changes())
    finish(board, review_b, approve())
    assert resumes_review(board, task, [PR]) is False
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_same_second_link_stays_held(board):
    task, review_a, _ = parked(board)
    finish(board, review_a, changes())
    parked_at = kb._to_epoch(board.execute(
        "SELECT created_at FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'",
        (task,)).fetchone()["created_at"])
    review_b = kb.create_task(board, title="review B", assignee="reviewer")
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(time, "time", lambda: parked_at)
        kb.link_tasks(board, review_b, task)
    finish(board, review_b, approve())
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("payload", ["{not json", '{"parent": 5}', "[1]"])
def test_unreadable_link_provenance_stays_held(board, payload):
    # A malformed linked event that might be B's earlier (pre-park) link keeps the hold,
    # although B's own post-park link is readable.
    task, _ = relinked(board)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'linked', ?, ?)",
                      (task, payload, int(time.time()) - 60))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_unreadable_link_time_stays_held(board):
    task, review_b = relinked(board)
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET created_at = 'garbage' WHERE task_id = ? AND kind = 'linked' "
                      "AND payload LIKE ?", (task, f'%{review_b}%'))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_archived_unrun_rereview_stays_held(board):
    # Archived counts as a satisfied parent, but it left no claimed completion.
    task, review_b = relinked(board, finish_b=False)
    assert kb.archive_task(board, review_b)
    kb.recompute_ready(board)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def link_events(conn, task, parent):
    return conn.execute("SELECT id FROM task_events WHERE task_id = ? AND kind = 'linked' AND payload LIKE ?",
                        (task, f"%{parent}%")).fetchall()


def test_rereview_with_a_pre_park_link_in_its_history_stays_held(board):
    # Linked before the park (then unlinked and relinked): not a parent added after it.
    task, review_b = relinked(board)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'linked', ?, ?)",
                      (task, json.dumps({"parent": review_b, "child": task}), int(time.time()) - 60))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_rereview_without_a_link_event_stays_held(board):
    task, review_b = relinked(board)
    assert len(link_events(board, task, review_b)) == 1
    with kb.write_txn(board):
        board.execute("DELETE FROM task_events WHERE id = ?", (link_events(board, task, review_b)[0]["id"],))
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("mangle", ["none", "removed", "duplicated", "missing"])
def test_checkpointed_parents_must_still_match_exactly(board, mangle):
    # Late parents relax only additions: a checkpointed parent that is gone, or a
    # checkpoint parent list that is not the sorted unique edge set, keeps the hold.
    task, review_a, _ = parked(board)
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? "
                        "AND kind = 'dependency_wait' ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    payload = json.loads(row["payload"])
    extra = {"none": [], "removed": ["t_gone"], "duplicated": [review_a], "missing": []}[mangle]
    payload["review_checkpoint"]["parents"] = sorted([review_a, *extra]) if mangle != "missing" else None
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    finish(board, review_a, changes(sha=HEAD))
    assert resumes_review(board, task, [PR]) is (mangle == "none")
    assert dispatch.check_respawn_guard(board, task) == (None if mangle == "none" else "active_pr")


def test_linked_rereview_joins_the_split_review_union(board, github_head):
    # Two checkpointed PRs: A requests changes on PR only, linked B approves PR2's
    # current head. Together they cover both PRs because B counts as an awaited parent.
    pr2 = PR.replace("/pull/10", "/pull/11")
    github_head[pr2] = "b" * 40
    task, review_a, _ = parked(board, prs=(PR, pr2))
    finish(board, review_a, changes())
    review_b = kb.create_task(board, title="review B", assignee="reviewer")
    kb.link_tasks(board, review_b, task)
    finish(board, review_b, approve(sha="b" * 40, pr=pr2))
    assert resumes_review(board, task, [PR, pr2]) is True
    assert dispatch.check_respawn_guard(board, task) is None


def test_pr_head_lookup_parses_and_caches(monkeypatch):
    calls = []

    class Proc:
        returncode, stdout = 0, json.dumps({"headRefOid": HEAD.upper()})

    monkeypatch.setattr(relink, "_gh_pr_head", _REAL_HEAD)
    monkeypatch.setattr(relink.subprocess, "run", lambda cmd, *a, **k: calls.append(cmd) or Proc())
    relink.clear_cache()
    assert relink.pr_head(PR) == HEAD
    assert relink.pr_head(PR) == HEAD
    assert len(calls) == 1 and calls[0][-4:] == ["view", PR, "--json", "headRefOid"]
