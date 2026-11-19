"""Storage observation and multi-episode controls independent of outer fences."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_containment_lifecycle as lifecycle
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_lifecycle as fixtures

board = fixtures.board


def test_marker_alone_retains_hold_without_observation_table(board):
    relay, _, _, _, _ = fixtures.setup(board)
    # Reach the task-page-before-history boundary without the redundant durable
    # observation table masking the live marker predicate.
    lifecycle.install_local(board)
    board.execute("DROP TRIGGER containment_observe_update")
    board.execute("DROP TRIGGER containment_observe_insert")
    board.execute("DROP TABLE containment_observations")
    assert not guard.pending(board, relay)
    episode = uuid7()
    board.execute("UPDATE tasks SET containment_episode=? WHERE id=?", (episode, relay))
    assert guard.pending(board, relay)


def test_no_uid_legacy_stop_is_held_without_portable_identity():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE task_events(task_id TEXT,kind TEXT,payload TEXT)")
        assert not guard.pending(conn, "fixture")
        conn.execute("INSERT INTO task_events VALUES ('fixture','contained','{}')")
        assert guard.pending(conn, "fixture")


def test_reducer_holds_known_stop_when_observed_marker_is_accounted_for(board):
    relay, _, _, target, spec = fixtures.setup(board)
    fixtures.contain(board, relay, target, spec)
    # Every observed episode has its matching stop, so only the reducer's
    # unresolved-release verdict can retain this hold.
    assert guard.pending(board, relay)
    assert board.execute("SELECT COUNT(*) FROM containment_observations WHERE task_id=?", (relay,)).fetchone()[0] == 1


def test_register_installs_delete_udf_on_fresh_connection(board):
    guard.register(board)
    assert board.execute("SELECT kanban_containment_delete('fixture')").fetchone()[0] == 0
    with sqlite3.connect(":memory:") as peer:
        guard.register(peer)
        assert peer.execute("SELECT kanban_containment_delete('fixture')").fetchone()[0] == 0


def test_inert_snapshot_neutralizes_delete_only_on_private_copy(board):
    relay, _, _, target, spec = fixtures.setup(board)
    episode = fixtures.contain(board, relay, target, spec)["episode"]
    with sqlite3.connect(":memory:") as private:
        board.backup(private)
        guard.inert_snapshot(private)
        assert private.execute("SELECT kanban_containment_delete(?)", (relay,)).fetchone()[0] == 0
        private.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
        assert private.execute("SELECT COUNT(*) FROM task_events WHERE uid=?", (episode,)).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))


def test_export_never_inerts_or_scrubs_source_connection(board, tmp_path, monkeypatch):
    from hermes_cli import kanban_transfer as transfer
    relay, _, authority, target, spec = fixtures.setup(board)
    fixtures.contain(board, relay, target, spec)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    original = guard.inert_snapshot
    seen = []

    def private_only(conn):
        private_path = conn.execute("PRAGMA database_list").fetchone()[2]
        assert private_path and private_path != path
        seen.append(private_path)
        original(conn)

    monkeypatch.setattr(guard, "inert_snapshot", private_only)
    assert transfer.export_board("default", str(tmp_path / "fixture.tar.gz"))
    assert len(seen) == 1
    row = kb.get_task(board, authority)
    assert row is not None and row.status == "running" and row.current_run_id is not None
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))


@pytest.mark.parametrize("case", ["own-not-running", "foreign-claimed"])
def test_target_defensive_custody_check_is_independent_of_actor_validation(board, monkeypatch, case):
    relay, consumer, authority, target, spec = fixtures.setup(board)
    # Isolate target custody from the upstream live-actor proof so either
    # rejection can independently defend an internal/recovery caller.
    monkeypatch.setattr(lifecycle, "_actor", lambda *_: None)
    if case == "own-not-running":
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
        actor = relay
        expected = "own containment requires"
    else:
        board.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (authority, relay))
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
        actor = authority
        expected = "unclaimed descendant"
    with pytest.raises(lifecycle.ContainmentRefused, match=expected):
        lifecycle.contain_local(board, target=target, resume_policy=spec, actor_task=actor,
                                actor_run_uid=uuid7(), summary="Fixture")
    assert kb.get_task(board, consumer).status == "todo"
    assert not board.execute("SELECT 1 FROM task_events WHERE kind='contained'").fetchone()


@pytest.mark.parametrize("fault", ["none", "unresolved", "membership", "missing-stop"])
def test_episode_rejection_occurs_before_any_release_write(board, monkeypatch, fault):
    from hermes_cli.kanban_containment_state import Hold
    relay, _, authority, target, spec = fixtures.setup(board)
    episode = fixtures.contain(board, relay, target, spec)["episode"]
    states = {"none": None, "unresolved": Hold((episode,), True),
              "membership": Hold((uuid7(),)), "missing-stop": Hold((episode,))}
    monkeypatch.setattr(lifecycle, "reduce_events", lambda *_a, **_k: states[fault])
    if fault == "missing-stop":
        monkeypatch.setattr(lifecycle, "_events", lambda *_: [])
    with pytest.raises(lifecycle.ContainmentRefused, match="unknown, stale or unresolved"):
        fixtures.release(board, target, episode, authority)
    assert not board.execute("SELECT 1 FROM task_events WHERE kind='containment_released'").fetchone()


def test_releasing_one_of_two_episodes_retains_triage_and_marker(board):
    relay, consumer, authority, target, spec = fixtures.setup(board)
    episode = fixtures.contain(board, relay, target, spec)["episode"]
    stop = lifecycle._stop(dict(board.execute("SELECT * FROM task_events WHERE uid=?", (episode,)).fetchone()))
    # Recovery can deliver more than one independently protected episode. Use
    # the internal storage seam to construct it without public repeat-contain.
    with kb.write_txn(board):
        with guard.authorized_write(board, relay):
            assert stop is not None
            second = lifecycle._append(board, relay, "contained", stop)
    assert second != episode
    result = fixtures.release(board, target, episode, authority)
    assert result["status"] == "triage"
    row = board.execute("SELECT status,containment_episode FROM tasks WHERE id=?", (relay,)).fetchone()
    assert tuple(row) == ("triage", episode)
    assert kb.get_task(board, consumer).status == "todo"
    assert guard.pending(board, relay, verify_release=fixtures.lifecycle_verifier(board))
    result = fixtures.release(board, target, second, authority)
    assert result["status"] == "ready"
    assert board.execute("SELECT containment_episode FROM tasks WHERE id=?", (relay,)).fetchone()[0] is None
    assert kb.get_task(board, consumer).status == "todo"
