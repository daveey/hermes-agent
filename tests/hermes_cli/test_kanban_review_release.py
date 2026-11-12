"""Home promotion evidence survives partial dependency replicas."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_dependency_review as helpers

board = helpers.board
PR = helpers.PR
OLD = PR + "9"


@pytest.mark.parametrize("missing", ["parent", "github"])
def test_home_promotion_releases_partial_replica(board, monkeypatch, missing):
    task, reviews = helpers.parked(board)
    # Captured older publication must be discharged as well as the reviewed PR.
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='dependency_wait'", (task,)).fetchone()
    import json
    data = json.loads(row["payload"])
    data["review_checkpoint"]["prs"].append(OLD)
    board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), row["id"]))
    helpers.worker_comment(board, task, "dev", OLD)
    board.commit()
    monkeypatch.setattr(prs, "pr_state", lambda url: "CLOSED" if url == OLD else "OPEN")
    helpers.finish_reviews(board, reviews)
    assert dispatch.check_respawn_guard(board, task) is None
    if missing == "parent":
        board.execute("UPDATE task_runs SET metadata=NULL WHERE task_id IN (?,?)", reviews)
        board.commit()
    else:
        monkeypatch.setattr(prs, "pr_state", lambda _: None)
    assert dispatch.check_respawn_guard(board, task) is None
    assert kb.claim_task(board, task) is not None
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


@pytest.mark.parametrize("mutation", ["head", "pr", "run", "absent", "parents"])
def test_invalid_receipt_does_not_release_unknown_replica(board, monkeypatch, mutation):
    import json
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    event = board.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='promoted' ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    data = json.loads(event["payload"])
    stamp = data["review_release"]
    changes = {
        "head": (stamp["reviews"][0], "head", "b" * 40),
        "pr": (stamp, "prs", [OLD]),
        "run": (stamp, "park", {"run_uid": "another-run"}),
        "parents": (stamp, "parents", []),
    }
    if mutation == "absent":
        data.pop("review_release")
    else:
        target, key, value = changes[mutation]
        target[key] = value
    board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), event["id"]))
    board.execute("DELETE FROM task_events WHERE task_id IN (?,?)", reviews)
    board.execute("DELETE FROM task_runs WHERE task_id IN (?,?)", reviews)
    board.commit()
    monkeypatch.setattr(prs, "pr_state", lambda _: None)
    assert dispatch.check_respawn_guard(board, task) == "active_pr"


def test_new_promotion_does_not_rearm_old_receipt(board, monkeypatch):
    from hermes_cli import kanban_review_release as release
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    kb._append_event(board, task, "promoted", {})
    board.commit()
    assert not release.released(board, task, {PR})


@pytest.mark.parametrize("field,value", [("reviewed_sha", "b" * 40), ("reviewed_pr", OLD)])
def test_same_result_contradiction_revokes_receipt(board, field, value):
    import json
    from hermes_cli import kanban_review_release as release
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    row = board.execute("SELECT id,metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (reviews[0],)).fetchone()
    metadata = json.loads(row["metadata"])
    metadata[field] = value
    board.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"]))
    board.commit()
    assert not release.released(board, task, {PR})


def test_lifecycle_retraction_revokes_receipt(board):
    from hermes_cli import kanban_review_release as release
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    kb._append_event(board, task, "descendant_invalidated", {})
    board.commit()
    assert not release.released(board, task, {PR})


def test_remote_review_change_during_precompute_drops_stamp(board, monkeypatch):
    from hermes_cli import kanban_review_release as release
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    board.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))
    board.commit()
    real = release._build
    lookup = release.review._completed_review_run

    def racing(conn, tid):
        stamp = real(conn, tid)
        # Model a Board shadow refresh while the external PR read ran.
        monkeypatch.setattr(release.review, "_completed_review_run", lambda *a: None)
        return stamp

    monkeypatch.setattr(release, "_build", racing)
    assert task not in release.prepare(board)
    monkeypatch.setattr(release.review, "_completed_review_run", lookup)
