"""Every release producer must exclude transfer custody before effects."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as transfer
from tests.hermes_cli import test_home_transfer_capture as support

board = support.board
PHASES = ("prepared", "pending", "malformed")


def freeze(conn, task, phase):
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (task,))
        conn.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
        conn.execute("DELETE FROM kanban_sync_outbox")
        if phase == "pending":
            from hermes_cli.kanban_home_generation import mirror_feed
            mirror_feed(conn, task, {"home_generation": "release-candidates-0001",
                                    "home_transfer_pending": "release-candidates-0001"}, 8)
        elif phase:
            transfer.prepare(conn, task, source="worker-a3", destination="hub",
                             nonce="release-candidates-0001", expected_version=7)
            if phase == "malformed":
                conn.execute("DROP TRIGGER home_prepared1_marker_update")
                conn.execute("UPDATE kanban_home_prepared1 SET nonce='invalid',source='' WHERE task_id=?", (task,))


def card(conn, status="triage"):
    task = kb.create_task(conn, title="release candidate", assignee="default")
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))
    return task


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("operation", ("promote", "unblock", "reopen", "admit", "release", "renew"))
def test_explicit_release_is_write_free(board, phase, operation):
    from hermes_cli import kanban_lanes as lanes, kanban_release as release
    from hermes_cli import kanban_scope_renewal as renewal
    status = "review" if operation == "reopen" else "triage"
    task = card(board, status)
    if operation == "admit":
        with dbc.write_txn(board):
            board.execute("UPDATE tasks SET block_kind=? WHERE id=?", (lanes.GATE_KIND, task))
    freeze(board, task, phase)
    before = board.total_changes
    def promote():
        ok, why = kb.promote_task(board, task, actor="test")
        return not ok and "home transfer" in (why or "")

    def released():
        result = release.release(board, task, writer=("human", None, "human"), runs=1)
        return result["outcome"] == "refused" and result["refusal"] == "home_transfer"

    def renewed():
        with pytest.raises(renewal.RenewalRefused) as exc:
            renewal.renew_local(board, task, episode="old", comment_id=1, reason="mandate",
                                actor=renewal.Actor("human", None, "human", None))
        return exc.value.code == "home_transfer"

    calls = {"promote": promote, "release": released, "renew": renewed,
             "unblock": lambda: kb.unblock_task(board, task) is False,
             "reopen": lambda: kb.reopen_review_task(board, task) is False,
             "admit": lambda: lanes.admit(board, task, actor="test", reason="capacity") is False}
    assert calls[operation]()
    assert board.total_changes == before
    assert kb.get_task(board, task).status == status


@pytest.mark.parametrize("operation", ("promote", "unblock", "reopen", "admit"))
def test_unfenced_release_remains_available(board, operation):
    from hermes_cli import kanban_lanes as lanes
    task = card(board, "review" if operation == "reopen" else "triage")
    if operation == "admit":
        with dbc.write_txn(board):
            board.execute("UPDATE tasks SET block_kind=? WHERE id=?", (lanes.GATE_KIND, task))
        assert lanes.admit(board, task, actor="test", reason="capacity")
    elif operation == "promote":
        assert kb.promote_task(board, task, actor="test") == (True, None)
    elif operation == "unblock":
        assert kb.unblock_task(board, task)
    else:
        assert kb.reopen_review_task(board, task)
    assert kb.get_task(board, task).status == "ready"


@pytest.mark.parametrize("phase", PHASES)
def test_dispatch_candidate_list_excludes_before_preclaim_side_effects(board, phase):
    from hermes_cli.kanban_db_dispatch_queries import lane_rows
    task, neighbour = card(board, "ready"), card(board, "ready")
    freeze(board, task, phase)
    before = board.total_changes
    assert [row["id"] for row in lane_rows(board, "ready")] == [neighbour]
    assert board.total_changes == before


@pytest.mark.parametrize("phase", PHASES)
def test_stale_triage_promotes_only_healthy_peer(board, phase):
    from hermes_cli import kanban_triage_sweep as sweep
    task, neighbour = card(board), card(board)
    freeze(board, task, phase)
    row = tuple(board.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())
    assert not sweep.awaiting_stale_sweep(board, task)
    assert sweep.sweep_stale_triage(board, older_than=0) == [neighbour]
    assert tuple(board.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()) == row
    assert kb.get_task(board, neighbour).status == "ready"
