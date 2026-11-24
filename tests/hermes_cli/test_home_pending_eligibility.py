"""A committed-home feed is not a native execution release before source STOP."""
from __future__ import annotations

import pytest
import sqlite3

from hermes_cli import kanban_db as kb, kanban_db_sync as sync, kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as transfer
from tests.hermes_cli import test_home_generation_feed as feed_support
from tests.hermes_cli import test_home_destination_materialize as dest_support

board = feed_support.board
NONCE = "pending-destination-0001"


def pending(conn, status="todo"):
    task = kb.create_task(conn, title="early destination feed")
    neighbour = kb.create_task(conn, title="independent")
    with dbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET gateway_id='hub'")
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (neighbour,))
    feed_support.feed(conn, task, home_transfer_pending=NONCE, nonce=NONCE)
    with dbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
    return task, neighbour


def test_early_feed_pending_cannot_promote_but_neighbour_can(board):
    task, neighbour = pending(board)
    assert kb.recompute_ready(board) == 1
    assert kb.get_task(board, task).status == "todo"
    assert kb.get_task(board, neighbour).status == "ready"
    assert transfer.frozen(board, task)


@pytest.mark.parametrize("review", [False, True])
def test_pending_claim_stops_before_external_lease(board, monkeypatch, review):
    task, _ = pending(board, "review" if review else "ready")
    monkeypatch.setattr(sync, "claim_gate", lambda *a, **k: pytest.fail("pending destination requested lease"))
    assert (kb.claim_review_task if review else kb.claim_task)(board, task) is None


def test_pending_wake_and_new_capture_refuse_until_newer_completion_feed(board):
    from hermes_cli.kanban_terminal_guard import skip_wake
    task, _ = pending(board, "scheduled")
    assert skip_wake(board, task, "scheduled")
    with pytest.raises(sqlite3.IntegrityError, match="home transfer pending"):
        kb.add_comment(board, task, "actor", "must not become delayed executable intent")
    with pytest.raises(ValueError, match="generation"):
        feed_support.feed(board, task, version=8, nonce=NONCE, home_transfer_pending=None)
    feed_support.feed(board, task, version=9, nonce=NONCE, home_transfer_pending=None)
    assert not transfer.frozen(board, task)
    kb.add_comment(board, task, "actor", "post STOP intent")


def test_actual_manifest_materialization_remains_ineligible(board):
    dest_support.prepare_destination(board)
    record = dest_support.manifest()
    record["rows"][0]["fields"]["home_transfer_pending"] = record["nonce"]
    dest_support.materialize(board, record)
    assert transfer.frozen(board, record["task"])


@pytest.mark.parametrize("ask", [False, True])
def test_actual_probe_restore_skips_pending_without_caching_it_as_settled(board, ask):
    from hermes_cli import kanban_wake_check as wake, kanban_ask_resolved as resolved
    from tests.hermes_cli.test_home_probe_custody import probe_manifest
    dest_support.prepare_destination(board)
    record = probe_manifest(host="hub", ask=ask)
    record["rows"][0]["fields"]["home_transfer_pending"] = record["nonce"]
    dest_support.materialize(board, record)
    repair = resolved.reconcile if ask else wake.restore_missing_checks
    before = board.total_changes
    assert repair(board, now=1001)["restored"] == []
    assert board.total_changes == before
    assert wake.get_state(board, record["task"]) is None
    dest_support.complete_feed(board, record)
    assert repair(board, now=1001)["restored"] == [record["task"]]


def test_pending_survives_existing_readonly_peer_and_missing_phase_is_not_release(board):
    import sqlite3
    dest_support.prepare_destination(board)
    record = dest_support.manifest()  # missing phase field is deliberately unknown
    dest_support.materialize(board, record)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as peer:
        assert transfer.frozen(peer, record["task"])
        dest_support.complete_feed(board, record)
        assert not transfer.frozen(peer, record["task"])
