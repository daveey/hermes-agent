"""Direct predicate controls isolate local evidence checks from public adapters."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_containment_lifecycle as lifecycle
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_lifecycle as fixtures

board = fixtures.board


def producer(conn):
    _, _, _, target, spec = fixtures.setup(conn)
    task = kb.create_task(conn, title="Producer fixture", assignee="default")
    spec["producer"] = {**target, "task_id": task}
    assert kb.claim_task(conn, task, claimer="default")
    run = kb.get_task(conn, task).current_run_id
    conn.execute("UPDATE task_runs SET uid=? WHERE id=?", (uuid7(), run))
    assert kb.complete_task(conn, task, summary="Fixture readiness")
    conn.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='completed'", (uuid7(), task))
    return task, run, target, spec


@pytest.mark.parametrize("key", ["authority", "producer"])
@pytest.mark.parametrize("fault", ["missing", "archived"])
def test_nominee_requires_existing_nonarchived_task(board, key, fault):
    relay, _, authority, target, spec = fixtures.setup(board)
    spec[key] = {**target, "task_id": authority}
    lifecycle._nominees(board, target, spec)
    if fault == "missing":
        spec[key]["task_id"] = "t_0123456789abcdef"
    else:
        board.execute("UPDATE tasks SET status='archived' WHERE id=?", (authority,))
    with pytest.raises(lifecycle.ContainmentRefused, match="must exist independently"):
        lifecycle._nominees(board, target, spec)
    assert kb.get_task(board, relay).status == "running"


@pytest.mark.parametrize("fault", ["status", "outcome", "ended_at", "newer"])
def test_producer_requires_current_terminal_completion(board, fault):
    task, run, _, spec = producer(board)
    original = lifecycle._producer(board, spec)
    assert original is not None
    if fault == "status":
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
    elif fault == "outcome":
        board.execute("UPDATE task_runs SET outcome='failed' WHERE id=?", (run,))
    elif fault == "ended_at":
        board.execute("UPDATE task_runs SET ended_at=NULL WHERE id=?", (run,))
    else:
        board.execute("INSERT INTO task_runs(task_id,uid,started_at,outcome,status) VALUES (?,?,1,'failed','failed')", (task, uuid7()))
    assert lifecycle._producer(board, spec) is None


@pytest.mark.parametrize("which", ["run", "event"])
def test_baseline_requires_portable_evidence_coordinates(board, which):
    task, run, _, spec = producer(board)
    assert lifecycle._baseline(board, spec)["run_uid"] is not None
    if which == "run":
        board.execute("UPDATE task_runs SET uid='invalid' WHERE id=?", (run,))
        expected = "baseline lacks a portable run identity"
    else:
        board.execute("UPDATE task_events SET uid='invalid' WHERE task_id=? AND kind='completed'", (task,))
        expected = "baseline lacks a portable event identity"
    with pytest.raises(lifecycle.ContainmentRefused, match=expected):
        lifecycle._baseline(board, spec)


@pytest.mark.parametrize("which", ["run", "event"])
def test_terminal_producer_requires_portable_coordinates(board, which):
    task, run, _, spec = producer(board)
    assert lifecycle._producer(board, spec) is not None
    if which == "run":
        board.execute("UPDATE task_runs SET uid='invalid' WHERE id=?", (run,))
    else:
        board.execute("UPDATE task_events SET uid='invalid' WHERE task_id=? AND kind='completed'", (task,))
    with pytest.raises(lifecycle.ContainmentRefused, match="terminal evidence lacks portable identities"):
        lifecycle._producer(board, spec)


def test_append_checks_actual_written_event_payload(board, monkeypatch):
    relay, *_ = fixtures.setup(board)
    original = kb._append_event
    def altered(conn, task, kind, payload):
        return original(conn, task, kind, {"different": True})
    monkeypatch.setattr(kb, "_append_event", altered)
    with pytest.raises(lifecycle.ContainmentRefused, match="unsupported containment event envelope"):
        lifecycle._append(board, relay, "fixture", {"expected": True})
    monkeypatch.setattr(kb, "_append_event", original)
    assert lifecycle._append(board, relay, "fixture", {"expected": True})


def test_actor_status_and_current_run_are_independent(board):
    relay, _, _, _, _ = fixtures.setup(board)
    uid = fixtures.run_uid(board, relay)
    lifecycle._actor(board, relay, uid)
    run = kb.get_task(board, relay).current_run_id
    board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
    with pytest.raises(lifecycle.ContainmentRefused, match="current live claimed run"):
        lifecycle._actor(board, relay, uid)
    board.execute("UPDATE tasks SET status='running',current_run_id=NULL WHERE id=?", (relay,))
    with pytest.raises(lifecycle.ContainmentRefused, match="current live claimed run"):
        lifecycle._actor(board, relay, uid)
    board.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run, relay))
    lifecycle._actor(board, relay, uid)


@pytest.mark.parametrize("fault", ["status", "run", "human", "kind"])
def test_resume_checks_current_custody_and_supported_kind(board, fault):
    relay, _, authority, target, spec = fixtures.setup(board)
    episode = fixtures.contain(board, relay, target, spec)["episode"]
    # Inject an impossible persisted row beneath the trigger to isolate the
    # lifecycle's defensive custody recheck. resume_local reinstalls the trigger.
    with kb.write_txn(board):
        board.execute("DROP TRIGGER containment_task_update")
        changes = {
            "status": "status='ready'", "run": "current_run_id=1",
            "human": "human_request='{}'", "kind": "status='triage'",
        }
        board.execute(f"UPDATE tasks SET {changes[fault]} WHERE id=?", (relay,))
    expected = "unsupported resume predicate" if fault == "kind" else "contained task changed custody"
    with pytest.raises(lifecycle.ContainmentRefused, match=expected):
        lifecycle.resume_local(board, target=target, episode=episode, actor_task=authority,
                               actor_run_uid=fixtures.run_uid(board, authority),
                               kind="invalid" if fault == "kind" else "authority")
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE kind='containment_released'").fetchone()[0] == 0
    with kb.write_txn(board):
        board.execute("DROP TRIGGER containment_task_update")
        board.execute("UPDATE tasks SET status='triage',current_run_id=NULL,human_request=NULL WHERE id=?", (relay,))
    assert fixtures.release(board, target, episode, authority)["status"] == "ready"



def test_producer_event_insertion_order_must_follow_stop(board):
    relay, consumer, authority, target, spec = fixtures.setup(board)
    task = kb.create_task(board, title="Producer fixture", assignee="default")
    spec["producer"] = {**target, "task_id": task}
    # Leave a genuine earlier free integer ID. Explicit-ID import/recovery can
    # replay older event positions; a newly generated UID alone is not recency.
    old_id = board.execute("SELECT MIN(id) FROM task_events WHERE task_id=?", (task,)).fetchone()[0]
    board.execute("DELETE FROM task_events WHERE id=?", (old_id,))
    episode = fixtures.contain(board, relay, target, spec)["episode"]
    assert kb.claim_task(board, task, claimer="default")
    run = kb.get_task(board, task).current_run_id
    board.execute("UPDATE task_runs SET uid=? WHERE id=?", (uuid7(), run))
    assert kb.complete_task(board, task, summary="Fixture readiness")
    event_id = board.execute("SELECT id FROM task_events WHERE task_id=? AND kind='completed'", (task,)).fetchone()[0]
    board.execute("UPDATE task_events SET uid=?,id=? WHERE id=?", (uuid7(), old_id, event_id))
    args = dict(target=target, episode=episode, actor_task=authority,
                actor_run_uid=fixtures.run_uid(board, authority), kind="producer")
    assert lifecycle._producer(board, spec) is not None
    with pytest.raises(lifecycle.ContainmentRefused, match="genuinely new completed-run evidence"):
        lifecycle.resume_local(board, **args)
    assert kb.get_task(board, consumer).status == "todo"
    board.execute("UPDATE task_events SET id=? WHERE id=?", (event_id, old_id))
    assert lifecycle.resume_local(board, **args)["status"] == "ready"
    assert kb.get_task(board, consumer).status == "todo"
