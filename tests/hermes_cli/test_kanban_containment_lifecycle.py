"""Transaction-level local containment integration; synced ingress stays gated."""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_legacy_paths as legacy

board = legacy.board


def setup(conn):
    from hermes_cli.kanban_db_sync import uuid7
    relay, consumer = legacy._relay(conn)
    authority = kb.create_task(conn, title="Authority fixture", assignee="default")
    assert kb.claim_task(conn, authority, claimer="default") is not None
    for table in ("task_events", "task_runs"):
        if "uid" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
        for row in conn.execute(f"SELECT id FROM {table} WHERE uid IS NULL").fetchall():
            conn.execute(f"UPDATE {table} SET uid=? WHERE id=?", (uuid7(), row[0]))
    target = {"board": "fixture", "gateway": "fixture-home", "task_id": relay}
    spec = {"version": 1, "authority": {**target, "task_id": authority}}
    return relay, consumer, authority, target, spec


def run_uid(conn, task):
    return conn.execute("SELECT uid FROM task_runs WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                        (task,)).fetchone()[0]


def contain(conn, relay, target, spec):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    return lifecycle.contain_local(conn, target=target, resume_policy=spec,
                                   actor_task=relay, actor_run_uid=run_uid(conn, relay), summary="Fixture containment")


def release(conn, target, episode, authority):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    return lifecycle.resume_local(conn, target=target, episode=episode, actor_task=authority,
                                  actor_run_uid=run_uid(conn, authority), kind="authority")


def test_local_containment_ends_only_run_preserving_dependency_and_counters(board):
    from hermes_cli import kanban_containment_guard as guard
    relay, consumer, authority, target, spec = setup(board)
    before = legacy._edges(board)
    board.execute("UPDATE tasks SET consecutive_failures=2 WHERE id=?", (relay,))
    rid = kb.get_task(board, relay).current_run_id
    result = contain(board, relay, target, spec)
    assert result["status"] == "triage"
    assert guard.pending(board, relay)
    assert kb.get_task(board, relay).current_run_id is None
    assert kb.get_task(board, consumer).status == "todo"
    assert legacy._edges(board) == before
    assert board.execute("SELECT consecutive_failures,human_request FROM tasks WHERE id=?", (relay,)).fetchone()[:] == (2, None)
    row = board.execute("SELECT outcome,ended_at FROM task_runs WHERE id=?", (rid,)).fetchone()
    assert row[0] == "contained" and row[1] is not None
    kinds = {r[0] for r in board.execute("SELECT kind FROM task_events WHERE task_id=?", (relay,))}
    assert not kinds.intersection({"completed", "review_requested", "scheduled", "blocked"})
    result = release(board, target, result["episode"], authority)
    assert result["status"] == "ready"
    assert guard.pending(board, relay, verify_release=lifecycle_verifier(board)) is False
    assert kb.get_task(board, consumer).status == "todo"
    assert legacy._edges(board) == before


def lifecycle_verifier(conn):
    from hermes_cli.kanban_containment_lifecycle import verified_release
    return lambda receipt, stop: verified_release(conn, receipt, stop)


@pytest.mark.parametrize("case", ["wrong_actor", "stale_run", "unknown_episode", "changed_policy"])
def test_resume_rejects_unauthorized_stale_or_tampered_evidence(board, case):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    relay, consumer, authority, target, spec = setup(board)
    episode = contain(board, relay, target, spec)["episode"]
    actor, uid = authority, run_uid(board, authority)
    if case == "wrong_actor":
        actor = consumer
    elif case == "stale_run":
        from hermes_cli.kanban_db_sync import uuid7
        uid = uuid7()
    elif case == "unknown_episode":
        from hermes_cli.kanban_db_sync import uuid7
        episode = uuid7()
    else:
        target = {**target, "board": "other"}
    with pytest.raises(lifecycle.ContainmentRefused):
        lifecycle.resume_local(board, target=target, episode=episode, actor_task=actor,
                               actor_run_uid=uid, kind="authority")
    assert kb.get_task(board, relay).status == "triage"
    assert kb.get_task(board, consumer).status == "todo"


@pytest.mark.parametrize("nominee", ["missing", "consumer"])
def test_authority_must_exist_and_cannot_depend_on_contained_task(board, nominee):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    relay, consumer, _, target, spec = setup(board)
    spec["authority"]["task_id"] = consumer if nominee == "consumer" else "t_0123456789abcdef"
    before = legacy._edges(board)
    with pytest.raises(lifecycle.ContainmentRefused):
        contain(board, relay, target, spec)
    assert kb.get_task(board, relay).status == "running"
    assert legacy._edges(board) == before


def test_restart_release_attestation_is_exact_and_generic_unblock_stays_refused(board):
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    relay, _, authority, target, spec = setup(board)
    episode = contain(board, relay, target, spec)["episode"]
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
    release(board, target, episode, authority)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path, isolation_level=None) as peer:
        peer.row_factory = sqlite3.Row
        lifecycle.install_local(peer)
        assert guard.pending(peer, relay, verify_release=lifecycle_verifier(peer)) is False
        stop = dict(peer.execute("SELECT * FROM task_events WHERE uid=?", (episode,)).fetchone())
        receipt = dict(peer.execute("SELECT * FROM task_events WHERE kind='containment_released'").fetchone())
        assert lifecycle.verified_release(peer, receipt, stop) is True
        receipt["payload"] = json.loads(receipt["payload"])
        receipt["payload"]["evidence"]["run_uid"] = episode
        assert lifecycle.verified_release(peer, receipt, stop) is False
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            peer.execute("DELETE FROM containment_attestations")


def test_changed_producer_requires_new_completed_run_and_current_terminal_event(board):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    from hermes_cli.kanban_db_sync import uuid7
    relay, consumer, authority, target, spec = setup(board)
    producer = kb.create_task(board, title="Producer fixture", assignee="default")
    spec["producer"] = {**target, "task_id": producer}
    assert kb.claim_task(board, producer, claimer="default")
    old_run = kb.get_task(board, producer).current_run_id
    board.execute("UPDATE task_runs SET uid=? WHERE id=?", (uuid7(), old_run))
    assert kb.complete_task(board, producer, summary="Old readiness")
    board.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='completed'", (uuid7(), producer))
    episode = contain(board, relay, target, spec)["episode"]
    args = dict(target=target, episode=episode, actor_task=authority,
                actor_run_uid=run_uid(board, authority), kind="producer")
    with pytest.raises(lifecycle.ContainmentRefused, match="new"):
        lifecycle.resume_local(board, **args)
    from hermes_cli.kanban_db_operator import reopen_done_task
    assert reopen_done_task(board, producer, reason="New fixture evidence", actor="fixture")
    assert kb.claim_task(board, producer, claimer="default")
    new_run = kb.get_task(board, producer).current_run_id
    board.execute("UPDATE task_runs SET uid=? WHERE id=?", (uuid7(), new_run))
    assert kb.complete_task(board, producer, summary="New readiness")
    board.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='completed' AND uid IS NULL", (uuid7(), producer))
    result = lifecycle.resume_local(board, **args)
    assert result["status"] == "ready"
    assert kb.get_task(board, consumer).status == "todo"


def test_synced_state_is_refused_before_any_schema_or_lifecycle_write(board):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    relay, _, _, target, spec = setup(board)
    board.execute("CREATE TABLE kanban_sync_state (id INTEGER)")
    board.execute("INSERT INTO kanban_sync_state VALUES (1)")
    before = board.total_changes
    with pytest.raises(lifecycle.ContainmentRefused, match="capability"):
        contain(board, relay, target, spec)
    assert board.total_changes == before


def test_failed_release_transaction_does_not_leave_a_durable_permission(board, monkeypatch):
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    from hermes_cli import kanban_containment_guard as guard
    relay, _, authority, target, spec = setup(board)
    episode = contain(board, relay, target, spec)["episode"]
    original = lifecycle._append
    def fail(conn, task_id, kind, payload):
        if kind == "containment_released":
            raise RuntimeError("fixture write failure")
        return original(conn, task_id, kind, payload)
    monkeypatch.setattr(lifecycle, "_append", fail)
    with pytest.raises(RuntimeError, match="fixture write failure"):
        release(board, target, episode, authority)
    assert board.execute("SELECT COUNT(*) FROM containment_attestations").fetchone()[0] == 0
    assert guard.pending(board, relay, verify_release=lifecycle_verifier(board))
    assert kb.get_task(board, relay).status == "triage"
