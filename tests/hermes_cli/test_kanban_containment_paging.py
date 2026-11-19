"""A canonical task marker must hold before its history page arrives."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_lifecycle as fixtures
from tests.hermes_cli import test_kanban_sync_capture as capture

board = fixtures.board
kanban_home = capture.kanban_home
synced_conn = capture.synced_conn


def test_marker_before_history_holds_generic_lifecycle_and_survives_restart(board):
    relay, _, _, _, _ = fixtures.setup(board)
    episode = uuid7()
    # Simulate the canonical task page before containment guards are installed.
    board.execute("UPDATE tasks SET status='triage',current_run_id=NULL,containment_episode=? WHERE id=?", (episode, relay))
    guard.install(board)
    assert guard.pending(board, relay)
    assert kb.get_task(board, relay).containment_episode == episode
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("UPDATE tasks SET containment_episode=NULL WHERE id=?", (relay,))
    with kb.write_txn(board), guard.authorized_write(board, relay):
        board.execute("UPDATE tasks SET containment_episode=NULL WHERE id=?", (relay,))
    assert guard.pending(board, relay)  # Bootstrapped observation survives a later task page.
    path = board.execute("PRAGMA database_list").fetchone()[2]
    again = sqlite3.connect(path)
    try:
        guard.install(again)
        assert guard.pending(again, relay)
    finally:
        again.close()


def test_later_task_page_cannot_erase_an_unresolved_observed_episode(board):
    relay, _, _, _, _ = fixtures.setup(board)
    guard.install(board)
    episode = uuid7()
    board.execute("UPDATE tasks SET status='triage',current_run_id=NULL,containment_episode=? WHERE id=?", (episode, relay))
    # Importing an authoritative task page will use a scoped sync seam. Even a
    # later cleared marker must not authorize before the missing history arrives.
    with kb.write_txn(board), guard.authorized_write(board, relay):
        board.execute("UPDATE tasks SET containment_episode=NULL WHERE id=?", (relay,))
    assert guard.pending(board, relay)
    assert board.execute("SELECT COUNT(*) FROM containment_observations WHERE task_id=?", (relay,)).fetchone()[0] == 1


def test_new_marker_is_not_satisfied_by_an_older_valid_release(board):
    relay, consumer, actor, target, spec = fixtures.setup(board)
    stop = fixtures.contain(board, relay, target, spec)
    from hermes_cli import kanban_containment_lifecycle as lifecycle
    released = lifecycle.resume_local(board, target=target, episode=stop["episode"], actor_task=actor,
                                     actor_run_uid=fixtures.run_uid(board, actor), kind="authority")
    assert released["status"] == "ready"
    assert not guard.pending(board, relay, verify_release=lambda r, s: lifecycle.verified_release(board, r, s))
    assert kb.get_task(board, relay).containment_episode is None
    assert kb.get_task(board, consumer).status == "todo"
    # The second episode's task page arrives before its stop event. The previous
    # authenticated release is valid, but is not authority for this new episode.
    board.execute("UPDATE tasks SET status='triage',containment_episode=? WHERE id=?", (uuid7(), relay))
    assert guard.pending(board, relay, verify_release=lambda r, s: lifecycle.verified_release(board, r, s))


def test_failed_trigger_upgrade_retains_prior_fence(board, monkeypatch):
    relay, _, _, _, _ = fixtures.setup(board)
    guard.install(board)
    board.execute("UPDATE tasks SET status='triage',current_run_id=NULL,containment_episode=? WHERE id=?", (uuid7(), relay))

    def interrupted(conn):
        conn.execute("DROP TRIGGER containment_task_update")
        raise sqlite3.OperationalError("fixture interrupted migration")

    # A settled install is read-only. Change a real input to request an upgrade.
    board.execute("ALTER TABLE task_events ADD COLUMN fixture_upgrade TEXT")
    monkeypatch.setattr(guard, "_triggers", interrupted)
    with pytest.raises(sqlite3.OperationalError, match="interrupted migration"):
        guard.install(board)
    assert not board.in_transaction
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (relay,))


def test_real_replica_apply_imports_hold_marker_but_outbox_never_authors_it(synced_conn):
    from hermes_cli import kanban_db_sync as sync
    conn = synced_conn
    tid = kb.create_task(conn, title="fixture metadata", assignee="default")
    episode = uuid7()
    capture._clear_outbox(conn)
    guard.install(conn)
    remote = sync.RemoteChange(1, "tasks", tid, "upsert",
                               {"id": tid, "status": "triage", "containment_episode": episode},
                               100, "", "fixture-home")
    sync.apply_remote_changes(conn, [remote], new_cursor=1, board="default")
    assert kb.get_task(conn, tid).containment_episode == episode
    assert guard.pending(conn, tid)
    assert sync.outbox(conn) == []
    # An annotation may still travel. Its full task row must omit the server
    # marker so a gateway cannot manufacture/clear canonical containment.
    conn.execute("UPDATE tasks SET priority=priority+1 WHERE id=?", (tid,))
    task_ops = [op for op in sync.outbox(conn) if op["table_name"] == "tasks"]
    assert task_ops
    assert all("containment_episode" not in op["fields"] for op in task_ops)
