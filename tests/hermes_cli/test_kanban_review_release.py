"""Home promotion evidence survives partial dependency replicas."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_dependency_review as helpers

board = helpers.board
PR = helpers.PR
OLD = PR + "9"


def as_replica(conn, task, home="home"):
    """View ``conn`` as a worker's synced copy of ``task`` (homed on ``home``) after promotion.

    Only a replica honours the home's stamp; the home itself re-evaluates. The
    synced schema names the park by its portable UID, so the stamp is restated
    with the UID the home would have carried (unsynced fixtures lack the column).
    """
    import json
    from hermes_cli import kanban_db_sync as sync
    conn.executescript(sync.SYNC_SCHEMA_SQL)
    conn.execute("INSERT INTO kanban_sync_state (board_id, gateway_id, hlc_last, mode) "
                 "SELECT 'board-test', 'worker', '', 'shadow' WHERE NOT EXISTS (SELECT 1 FROM kanban_sync_state)")
    for table, column in (("tasks", "created_on"), ("task_runs", "uid")):
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (home, task))
    conn.execute("UPDATE task_events SET origin_gateway=? WHERE task_id=? AND kind='promoted'", (home, task))
    for event in conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='promoted'",
                              (task,)).fetchall():
        payload = json.loads(event["payload"] or "{}")
        park = (payload.get("review_release") or {}).get("park") if isinstance(payload, dict) else None
        if isinstance(park, dict) and "run_id" in park:
            uid = f"park-{park['run_id']}"
            conn.execute("UPDATE task_runs SET uid=? WHERE id=?", (uid, park["run_id"]))
            payload["review_release"]["park"] = {"run_uid": uid}
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event["id"]))
    conn.commit()


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
    as_replica(board, task)
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
    as_replica(board, task)
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
    as_replica(board, task)
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
    as_replica(board, task)
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


@pytest.mark.parametrize("hold,evaluated", [
    ("sticky", False), ("breaker", False), ("task_breaker", False), ("task_headroom", True), ("none", True),
    ("todo_at_limit", True)])
def test_held_card_gets_no_external_evaluation(board, monkeypatch, hold, evaluated):
    # recompute_ready never promotes a sticky or breaker-held card, so its precompute
    # must not spend GitHub reads or snapshots on it either. The breaker binds only
    # blocked/triage stops: a todo dependency wait at the limit still promotes.
    import json
    from hermes_cli.kanban_block_routing import has_sticky_block
    task, reviews = helpers.parked(board)
    limit = kb.DEFAULT_FAILURE_LIMIT
    status = "todo" if hold == "todo_at_limit" else "blocked"
    failures, retries = {"breaker": (limit, None), "task_breaker": (1, 1), "todo_at_limit": (limit, None),
                         "task_headroom": (limit, limit + 1)}.get(hold, (0, None))
    board.execute("UPDATE tasks SET status=?, consecutive_failures=?, max_retries=? WHERE id=?",
                  (status, failures, retries, task))
    if hold == "sticky":
        kb._append_event(board, task, "blocked", {"reason": "explicit"})
    board.commit()
    assert has_sticky_block(board, task) is (hold == "sticky")
    reads = []
    monkeypatch.setattr(prs, "pr_state", lambda url: reads.append(url) or "OPEN")
    for review in reviews:
        kb.claim_task(board, review)
        run = kb.get_task(board, review).current_run_id
        assert kb.complete_task(board, review, summary="Review complete", expected_run_id=run,
                                metadata={"verdict": "changes_required", "pr": PR, "reviewed_sha": "a" * 40})
    board.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
    board.commit()
    reads.clear()
    kb.recompute_ready(board)
    assert bool(reads) is evaluated
    promoted = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='promoted'",
                             (task,)).fetchall()
    assert bool(promoted) is evaluated
    if evaluated:
        assert "review_release" in json.loads(promoted[-1]["payload"])


def test_home_never_honours_its_own_stamp(board):
    # The home holds the evidence and keeps deciding with resumes_review, so a
    # later change to that evidence re-holds there; only a replica uses the stamp.
    import json
    from hermes_cli import kanban_review_release as release
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    payload = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='promoted' "
                            "ORDER BY id DESC LIMIT 1", (task,)).fetchone()["payload"]
    assert "review_release" in json.loads(payload)
    assert not release.released(board, task, {PR})
    as_replica(board, task)
    assert release.released(board, task, {PR})
