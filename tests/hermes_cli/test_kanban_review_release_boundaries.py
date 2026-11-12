"""Receipt field boundaries independently reject malformed continuation grants."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_release as release
from tests.hermes_cli import test_kanban_dependency_review as helpers

board = helpers.board
PR = helpers.PR


@pytest.mark.parametrize("section,key,value", [
    ("stamp", "version", 2),
    ("stamp", "scope", "forever"),
    ("stamp", "merge_authorized", True),
    ("stamp", "checkpoint", {}),
    ("stamp", "discharged", {PR: True}),
    ("stamp", "discharged", {}),
    ("stamp", "discharged", [123]),
    ("stamp", "discharged", [[PR]]),
    ("stamp", "discharged", [PR + "9"]),
    ("stamp", "discharged", [PR]),
    ("stamp", "reviews", {}),
    ("stamp", "reviews", None),
    ("stamp", "reviews", []),
    ("stamp", "reviews", [None]),
    ("proof", "metadata", []),
    ("proof", "head", 123),
    ("proof", "head", "short"),
    ("proof", "parent", "other-parent"),
    ("proof", "prs", {PR: True}),
    ("proof", "prs", [123]),
    ("proof", "prs", [[PR]]),
    ("proof", "prs", [PR + "9"]),
    ("proof", "result", {"run_uid": "other-result"}),
])
def test_corrupt_receipt_is_not_authority(board, section, key, value):
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    event = board.execute("SELECT id,payload FROM task_events WHERE task_id=? "
                          "AND kind='promoted' ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    payload = json.loads(event["payload"])
    stamp = payload["review_release"]
    # Retain one proof so a later redundant proof cannot mask a malformed one.
    stamp["reviews"] = stamp["reviews"][:1]
    target = stamp if section == "stamp" else stamp["reviews"][0]
    target[key] = value
    board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
    board.commit()
    assert not release.released(board, task, {PR}), (section, key, value)


@pytest.mark.parametrize("defect", ["reopened", "origin", "old_promotion", "new_pr", "no_promotion"])
def test_lifecycle_and_scope_boundaries(board, defect):
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    urls = {PR, PR + "9"} if defect == "new_pr" else {PR}
    board.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
    board.execute("UPDATE tasks SET created_on='home' WHERE id=?", (task,))
    mutations = {
        "reopened": ("UPDATE tasks SET status='todo' WHERE id=?", reviews[0]),
        "origin": ("UPDATE task_events SET origin_gateway='foreign' WHERE task_id=? AND kind='promoted'", task),
        "old_promotion": ("UPDATE task_events SET created_at='2000-01-01T00:00:00Z' WHERE task_id=? AND kind='promoted'", task),
        "no_promotion": ("DELETE FROM task_events WHERE task_id=? AND kind='promoted'", task),
        "new_pr": ("SELECT ?", task),
    }
    sql, target = mutations[defect]
    board.execute(sql, (target,))
    board.commit()
    assert not release.released(board, task, urls)


@pytest.mark.parametrize("fence", ["foreign", "pending", "unauthenticated"])
def test_prepare_never_evaluates_ineligible_owner(board, monkeypatch, fence):
    from hermes_cli import kanban_wake_check
    task, reviews = helpers.parked(board, authenticated=fence != "unauthenticated")
    if fence != "pending":
        helpers.finish_reviews(board, reviews)
    board.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))
    board.commit()
    if fence == "foreign":
        monkeypatch.setattr(kanban_wake_check, "foreign_ids", lambda conn: {task})

    def forbidden(conn, tid):
        pytest.fail("ineligible owner reached external release evaluation")

    monkeypatch.setattr(release, "_build", forbidden)
    assert task not in release.prepare(board)


def test_promotion_transaction_rechecks_prepared_evidence(board):
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    board.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))
    board.commit()
    prepared = release.prepare(board)
    assert task in prepared
    assert "review_release" in release.promotion_payload(board, task, "ready", prepared)
    kb.add_comment(board, task, "reviewer", "Evidence changed during precompute")
    assert release.promotion_payload(board, task, "ready", prepared) is None


@pytest.mark.parametrize("park_delay", [0, 5])
def test_checkpoint_comment_before_park_releases_partial_replica(board, monkeypatch, park_delay):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_db_dispatch_pr_state as prs
    task, reviews = helpers.parked(board, park_delay=park_delay)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    board.execute("DELETE FROM task_events WHERE task_id IN (?,?)", reviews)
    board.execute("DELETE FROM task_runs WHERE task_id IN (?,?)", reviews)
    board.commit()
    monkeypatch.setattr(prs, "pr_state", lambda _: None)
    assert dispatch.check_respawn_guard(board, task) is None
