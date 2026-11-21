"""An orderly ordering-parent park resumes its PR once; failures never do."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.kanban_guard_helpers import worker_comment

PR = "https://github.com/example/repo/pull/10"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setattr(prs, "pr_state", lambda _: "OPEN")
    from hermes_cli.kanban_model_assignment import _assignee_config
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (tmp_path / "config.yaml").write_text(_managed_yaml())
    profile = tmp_path / "profiles" / "dev"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(_managed_yaml())
    assert _assignee_config("dev")["model_routing"]["enabled"] is True
    kb.init_db()
    with kbc.connect() as conn:
        assert str(conn.execute("PRAGMA database_list").fetchone()[2]).startswith(str(tmp_path))
        yield conn


def park(conn, monkeypatch, *, delta=10, finish=True, owned=True):
    now = int(time.time())
    clock = [now]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(conn, title="finish existing PR", assignee="dev")
    kb.claim_task(conn, task)
    run = kb.get_task(conn, task).current_run_id
    worker_comment(conn, task, "dev", PR)
    parent = kb.create_task(conn, title="ordering prerequisite", assignee="dev")
    kb.link_tasks(conn, parent, task, expected_child_run_id=run)
    clock[0] += delta
    assert kb.block_task(conn, task, kind="dependency", reason="ordering prerequisite",
                         expected_run_id=run if owned else None)
    if finish:
        clock[0] += 10
        kb.claim_task(conn, parent)
        assert kb.complete_task(conn, parent, summary="ordering work finished")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, task).status == "ready"
    return task, run, clock


def test_ordering_dependency_dispatches_once(board, monkeypatch, all_assignees_spawnable):
    task, _, _ = park(board, monkeypatch)
    assert prs.active_pr_reason(board, task, int(time.time())) is None
    launched = []
    result = dispatch.dispatch_once(board, spawn_fn=lambda task, workspace, **kwargs: launched.append(task.id) or 123456789)
    assert task in launched, result
    assert len(result.spawned) == 1
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"
    assert kb.reclaim_task(board, task, reason="successor crashed", signal_fn=lambda *_: None)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("kind", ["crashed", "reclaimed", "timed_out"])
def test_failure_after_ordering_park_stays_guarded(board, monkeypatch, kind):
    task, run, clock = park(board, monkeypatch)
    clock[0] += 1
    with kb.write_txn(board):
        kb._append_event(board, task, kind, {}, run_id=run)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_park_before_new_publication_then_crash_stays_guarded(board, monkeypatch):
    task, run, clock = park(board, monkeypatch)
    clock[0] += 1
    worker_comment(board, task, "dev", PR)
    with kb.write_txn(board):
        kb._append_event(board, task, "crashed", {}, run_id=run)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_same_second_park_after_own_pr_report_resumes_once(board, monkeypatch, all_assignees_spawnable):
    # The owner posts its state comment (with the PR URL) and parks in the same second.
    # The park's server-stamped comment watermark orders the two, so this is the
    # orderly handoff, not a duplicate-publication risk.
    task, _, _ = park(board, monkeypatch, delta=0)
    evidence = kb._json_dict(board.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' "
        "ORDER BY id DESC LIMIT 1", (task,)).fetchone()[0])["review_checkpoint"]
    latest = board.execute("SELECT MAX(id) FROM task_comments WHERE task_id = ?", (task,)).fetchone()[0]
    assert evidence["comment_id_at_checkpoint"] == latest
    assert prs.active_pr_reason(board, task, int(time.time())) is None
    result = dispatch.dispatch_once(board, spawn_fn=lambda task, workspace, **kwargs: 123456789)
    assert len(result.spawned) == 1
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_same_second_pr_comment_after_park_stays_guarded(board, monkeypatch):
    # A PR comment in the park's second but after it is beyond the watermark.
    now = int(time.time())
    clock = [now]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(board, title="finish existing PR", assignee="dev")
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    parent = kb.create_task(board, title="ordering prerequisite", assignee="dev")
    kb.link_tasks(board, parent, task, expected_child_run_id=run)
    clock[0] += 10
    worker_comment(board, task, "dev", PR)
    assert kb.block_task(board, task, kind="dependency", reason="ordering prerequisite", expected_run_id=run)
    worker_comment(board, task, "dev", "https://github.com/example/repo/pull/11")
    clock[0] += 10
    kb.claim_task(board, parent)
    assert kb.complete_task(board, parent, summary="ordering work finished")
    kb.recompute_ready(board)
    assert kb.get_task(board, task).status == "ready"
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("watermark", [None, 0, "7", 10**9])
def test_same_second_park_without_valid_watermark_stays_guarded(board, monkeypatch, watermark):
    task, _, _ = park(board, monkeypatch, delta=0)
    row = board.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' "
                        "ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    payload = kb._json_dict(row["payload"])
    if watermark is None:
        payload["review_checkpoint"].pop("comment_id_at_checkpoint")
    else:
        payload["review_checkpoint"]["comment_id_at_checkpoint"] = watermark
    import json
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_unfinished_parent_does_not_release(board, monkeypatch):
    task, _, _ = park(board, monkeypatch, finish=False)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("mutation", ["wrong_profile", "missing_end", "bad_outcome", "no_promotion", "no_run"])
def test_missing_handoff_evidence_stays_guarded(board, monkeypatch, mutation):
    task, run, _ = park(board, monkeypatch)
    mutations = {
        "wrong_profile": ("UPDATE task_runs SET profile = 'other' WHERE id = ?", run),
        "missing_end": ("UPDATE task_runs SET ended_at = NULL WHERE id = ?", run),
        "bad_outcome": ("UPDATE task_runs SET outcome = 'crashed' WHERE id = ?", run),
        "no_promotion": ("DELETE FROM task_events WHERE task_id = ? AND kind = 'promoted'", task),
        "no_run": ("UPDATE task_events SET run_id = NULL WHERE task_id = ? AND kind = 'dependency_wait'", task),
    }
    sql, value = mutations[mutation]
    with kb.write_txn(board):
        board.execute(sql, (value,))
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("offset,expected", [(-1, None), (0, "active_pr"), (1, "active_pr")])
def test_imported_failure_uses_occurrence_time(board, monkeypatch, offset, expected):
    from datetime import UTC, datetime

    task, run, clock = park(board, monkeypatch)
    clock[0] = clock[0] - 10 + offset
    with kb.write_txn(board):
        kb._append_event(board, task, "crashed", {}, run_id=run)
        board.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'crashed'",
                      (datetime.fromtimestamp(clock[0], UTC).isoformat(), task))
    clock[0] += 30
    assert prs.active_pr_reason(board, task, int(time.time())) == expected


def test_operator_link_without_owner_run_stays_guarded(board, monkeypatch):
    task, _, clock = park(board, monkeypatch)
    kb.claim_task(board, task)
    kb.reclaim_task(board, task, reason="worker died", signal_fn=lambda *_: None)
    clock[0] += 1
    parent = kb.create_task(board, title="operator ordering", assignee="dev")
    kb.link_tasks(board, parent, task)
    clock[0] += 1
    kb.complete_task(board, parent, summary="ordering finished")
    kb.recompute_ready(board)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("metadata", [
    {"verdict": "approved", "head": "short"},
    {"verdict": "changes_required", "reviewed_sha": "a" * 40, "pr": PR + "1"},
])
def test_later_park_cannot_bypass_review_evidence(board, monkeypatch, metadata):
    from tests.hermes_cli.test_kanban_dependency_review import finish_reviews

    task, _, clock = park(board, monkeypatch, finish=False)
    clock[0] += 10
    finish_reviews(board, kb.parent_ids(board, task), metadata=metadata)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_operator_park_of_live_owner_run_is_not_owner_intent(board, monkeypatch):
    task, _, _ = park(board, monkeypatch, owned=False)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_removing_awaited_edge_is_not_parent_completion(board, monkeypatch):
    task, _, clock = park(board, monkeypatch, finish=False)
    awaited = kb.parent_ids(board, task)[0]
    old = kb.create_task(board, title="already done", assignee="dev")
    kb.complete_task(board, old, summary="done")
    kb.link_tasks(board, old, task)
    clock[0] += 10
    kb.unlink_tasks(board, awaited, task)
    kb.recompute_ready(board)
    assert kb.get_task(board, task).status == "ready"
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def _add_gate(board, task, clock, *, finish=True, metadata=None):
    clock[0] += 5
    gate = kb.create_task(board, title="operator-added gate", assignee="dev")
    kb.link_tasks(board, gate, task)
    kb.recompute_ready(board)
    if finish:
        clock[0] += 5
        kb.claim_task(board, gate)
        assert kb.complete_task(board, gate, summary="gate finished", metadata=metadata or {"head": "a" * 40})
        kb.recompute_ready(board)
    return gate


def test_operator_added_gate_after_owner_park_still_allows_one_continuation(board, monkeypatch,
                                                                            all_assignees_spawnable):
    task, _, clock = park(board, monkeypatch)
    _add_gate(board, task, clock)
    assert kb.get_task(board, task).status == "ready"
    assert prs.active_pr_reason(board, task, int(time.time())) is None
    launched = []
    dispatch.dispatch_once(board, spawn_fn=lambda task, workspace, **kwargs: launched.append(task.id) or 123456789)
    assert task in launched
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_operator_added_gate_still_open_stays_guarded(board, monkeypatch):
    task, _, clock = park(board, monkeypatch)
    _add_gate(board, task, clock, finish=False)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_operator_added_review_gate_cannot_bypass_review_evidence(board, monkeypatch):
    task, _, clock = park(board, monkeypatch)
    _add_gate(board, task, clock, metadata={"verdict": "approved", "head": "a" * 40})
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def _remote_parent(board, task, run, status):
    from hermes_cli import kanban_db_sync as sync

    board.executescript(sync.SYNC_SCHEMA_SQL)
    with kb.write_txn(board):
        board.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", ("t_remote_done", task))
        board.execute("INSERT INTO kanban_sync_remote_parents (parent_id, status, refreshed_at) "
                      "VALUES ('t_remote_done', ?, 1)", (status,))


def _park_with_remote(board, monkeypatch, status):
    now = int(time.time())
    clock = [now]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(board, title="finish existing PR", assignee="dev")
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    worker_comment(board, task, "dev", PR)
    _remote_parent(board, task, run, status)
    parent = kb.create_task(board, title="ordering prerequisite", assignee="dev")
    kb.link_tasks(board, parent, task, expected_child_run_id=run)
    clock[0] += 10
    assert kb.block_task(board, task, kind="dependency", reason="ordering", expected_run_id=run)
    clock[0] += 10
    kb.claim_task(board, parent)
    assert kb.complete_task(board, parent, summary="ordering work finished")
    return task, clock


def test_synced_parent_done_before_park_does_not_hold_ordering_continuation(board, monkeypatch):
    task, _ = _park_with_remote(board, monkeypatch, "done")
    kb.recompute_ready(board)
    assert kb.get_task(board, task).status == "ready"
    assert prs.active_pr_reason(board, task, int(time.time())) is None


def test_synced_parent_awaited_at_park_stays_guarded(board, monkeypatch):
    task, _ = _park_with_remote(board, monkeypatch, "running")
    with kb.write_txn(board):
        board.execute("UPDATE kanban_sync_remote_parents SET status = 'done' WHERE parent_id = 't_remote_done'")
    kb.recompute_ready(board)
    assert kb.get_task(board, task).status == "ready"
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


def test_prose_review_without_head_still_requires_review_evidence(board, monkeypatch):
    task, _, clock = park(board, monkeypatch, finish=False)
    clock[0] += 10
    parent = kb.parent_ids(board, task)[0]
    kb.claim_task(board, parent)
    kb.complete_task(board, parent, summary="VERDICT: REQUEST_CHANGES", metadata={})
    kb.recompute_ready(board)
    assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"


@pytest.mark.parametrize("approved_before_park", [True, False], ids=["approved_before_park", "approved_after_park"])
def test_review_parent_finished_before_the_park_does_not_hold_the_ordering_continuation(
        board, monkeypatch, approved_before_park):
    """2026-11-13 t_01a1d09d3547337b (state PR #1760): its exact-head approval finished,
    then the owner parked on a fleet-rollout parent; when the rollout finished the
    finished approval parent still refused the ordering handoff (held 30 min). A review
    parent that finishes after the park is awaited and stays on resumes_review's rules."""
    now = int(time.time())
    clock = [now]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    task = kb.create_task(board, title="finish existing PR", assignee="dev")
    kb.claim_task(board, task)
    run = kb.get_task(board, task).current_run_id
    worker_comment(board, task, "dev", PR)
    review = kb.create_task(board, title="exact-head review", assignee="reviewer")
    kb.link_tasks(board, review, task, expected_child_run_id=run)

    def approve():
        kb.claim_task(board, review)
        assert kb.complete_task(board, review, summary="Approved", metadata={
            "verdict": "approve", "reviewed_sha": "a" * 40, "pr": PR})

    if approved_before_park:
        clock[0] += 5
        approve()
    rollout = kb.create_task(board, title="fleet rollout", assignee="dev")
    kb.link_tasks(board, rollout, task, expected_child_run_id=run)
    clock[0] += 10
    assert kb.block_task(board, task, kind="dependency", reason="rollout", expected_run_id=run)
    clock[0] += 10
    if not approved_before_park:
        approve()
        clock[0] += 5
    kb.claim_task(board, rollout)
    assert kb.complete_task(board, rollout, summary="rollout finished")
    kb.recompute_ready(board)
    assert kb.get_task(board, task).status == "ready"
    if approved_before_park:
        assert prs.active_pr_reason(board, task, int(time.time())) is None
        kb.claim_task(board, task)
        assert prs.active_pr_reason(board, task, int(time.time())) == "active_pr"
    else:
        # A review result awaited at the park belongs to resumes_review (parent approval
        # names the PR at a full head: it resumes there, not via the ordering handoff).
        assert prs._dependency_handoff_after(board, task, now, 0) is False
