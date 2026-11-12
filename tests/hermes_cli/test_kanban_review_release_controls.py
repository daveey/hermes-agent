"""Producer and consumer controls for the home review-release receipt.

Each case isolates one guard: the home emits a receipt only for evidence it can
carry, and a replica without the parent's run/result still rejects a receipt
whose own fields do not prove a continuation. Malformed data never raises.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_release as release
from tests.hermes_cli import test_kanban_dependency_review as helpers

board = helpers.board
PR = helpers.PR
OLD = PR + "9"


def _waiting(conn, reviews_metadata=None):
    task, reviews = helpers.parked(conn)
    if reviews_metadata is None:
        helpers.finish_reviews(conn, reviews)
    else:
        for review, metadata in zip(reviews, reviews_metadata):
            helpers.finish_reviews(conn, [review], metadata=metadata)
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))
    conn.commit()
    return task, reviews


def _set_checkpoint_prs(conn, task, value):
    row = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='dependency_wait' "
                       "ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    data = json.loads(row["payload"])
    data["review_checkpoint"]["prs"] = value
    conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), row["id"]))


def _promoted(conn, task):
    event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='promoted' "
                         "ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    return event["id"], json.loads(event["payload"])


def _store(conn, event_id, payload):
    conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event_id))
    conn.commit()


def _drop_parent_history(conn, reviews):
    conn.execute("DELETE FROM task_events WHERE task_id IN (?,?)", reviews)
    conn.execute("DELETE FROM task_runs WHERE task_id IN (?,?)", reviews)
    conn.commit()


# --- producer: the home stamps only its own release verdict ---------------------------

def test_home_hold_verdict_emits_no_receipt(board, monkeypatch):
    task, _ = _waiting(board)
    assert task in release.prepare(board)
    monkeypatch.setattr(release.review, "resumes_review", lambda *a, **k: False)
    assert task not in release.prepare(board)


def test_release_without_canonical_head_stays_local(board):
    # Mixed prefix findings release on the home's own evaluator, but carry no
    # single exact head, so no portable receipt is emitted for replicas.
    prefix = {"verdicts": {"prefix1": "REQUEST_CHANGES"}, "reviewed_sha_prefix1": "a" * 40, "pr": PR}
    task, _ = _waiting(board, [prefix, prefix])
    assert release.review.resumes_review(board, task, {PR})
    assert task not in release.prepare(board)


def test_reviewer_of_another_pr_adds_no_proof(board):
    other = "https://github.com/example/repo/pull/99"
    task, reviews = _waiting(board, [None, {"verdict": "changes_required", "pr": other, "reviewed_sha": "a" * 40}])
    stamp = release.prepare(board)[task][1]
    assert [p["parent"] for p in stamp["reviews"]] == [reviews[0]]


@pytest.mark.parametrize("value", [None, [123], {PR: True}])
def test_malformed_checkpoint_scope_emits_no_receipt(board, value):
    task, _ = _waiting(board)
    _set_checkpoint_prs(board, task, value)
    board.commit()
    assert task not in release.prepare(board)


def test_parent_without_carryable_result_adds_no_proof(board, monkeypatch):
    task, reviews = _waiting(board)
    stamp = release.prepare(board)[task][1]
    assert sorted(p["parent"] for p in stamp["reviews"]) == sorted(reviews)
    real = release.review._completed_review_run

    def only_first(conn, parent, *rest):
        # A parent whose result this store cannot read contributes no proof; the
        # other reviewer's exact-head proof still covers the captured PR.
        return real(conn, parent, *rest) if parent == reviews[0] else None

    monkeypatch.setattr(release.review, "_completed_review_run", only_first)
    stamp = release.prepare(board)[task][1]
    assert [p["parent"] for p in stamp["reviews"]] == [reviews[0]]


# --- consumer: partial replica (no parent run/result) checks the receipt itself -------

@pytest.mark.parametrize("case", ["implicit", "short_head", "extra_pr", "explicit_other"])
def test_partial_replica_checks_proof_fields(board, case):
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    event_id, payload = _promoted(board, task)
    stamp = payload["review_release"]
    stamp["reviews"] = stamp["reviews"][:1]
    proof = stamp["reviews"][0]
    proof["metadata"].pop("pr", None)
    proof["metadata"].pop("reviewed_pr", None)
    if case == "short_head":
        proof["head"] = proof["metadata"]["reviewed_sha"] = "a" * 12
    elif case == "extra_pr":
        proof["prs"] = [PR, OLD]
    elif case == "explicit_other":
        proof["metadata"]["pr"] = OLD
    _store(board, event_id, payload)
    _drop_parent_history(board, reviews)
    assert release.released(board, task, {PR}) is (case == "implicit")


@pytest.mark.parametrize("case", ["uid", "legacy_park", "no_origin"])
def test_foreign_copy_needs_home_origin_and_portable_park(board, monkeypatch, case):
    from hermes_cli import kanban_wake_check
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    board.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
    board.execute("UPDATE tasks SET created_on='home' WHERE id=?", (task,))
    if case != "no_origin":
        board.execute("UPDATE task_events SET origin_gateway='home' WHERE task_id=? AND kind='promoted'", (task,))
    event_id, payload = _promoted(board, task)
    run = board.execute("SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    assert payload["review_release"]["park"] == {"run_id": run["id"]}
    if case != "legacy_park":
        # A synced copy names the park by its portable UID, never a local integer
        # (unsynced fixtures lack the synced schema's uid column).
        board.execute("ALTER TABLE task_runs ADD COLUMN uid TEXT")
        board.execute("UPDATE task_runs SET uid='owner-park' WHERE id=?", (run["id"],))
        payload["review_release"]["park"] = {"run_uid": "owner-park"}
    _store(board, event_id, payload)
    monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda conn, tid: True)
    assert release.released(board, task, {PR}) is (case == "uid")


@pytest.mark.parametrize("value", [None, [123], {PR: True}])
def test_malformed_checkpoint_scope_is_not_released(board, value):
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    _set_checkpoint_prs(board, task, value)
    event_id, payload = _promoted(board, task)
    payload["review_release"]["checkpoint"]["prs"] = value
    _store(board, event_id, payload)
    assert release.released(board, task, {PR}) is False


@pytest.mark.parametrize("fault", ["crashed", "reclaimed", "timed_out"])
def test_owner_fault_after_park_revokes_receipt(board, fault):
    task, reviews = helpers.parked(board)
    helpers.finish_reviews(board, reviews)
    assert release.released(board, task, {PR})
    kb._append_event(board, task, fault, {})
    board.commit()
    assert not release.released(board, task, {PR})
