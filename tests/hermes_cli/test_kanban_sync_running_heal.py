"""A card the Board already reads ``running`` over its ended run is healed by its owner's sync round. [daveey]

Hub 2026-11-12: t_01a1ceaeb6ba593c (completed) and t_01a1ceb44e35f780 (blocked -> todo) were regressed on the Board
itself (v21 / v11), so the replica and the live file agreed and no divergence repair could see them."""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_sync_divergence import heal_running_over_ended_run
from tests.hermes_cli import test_kanban_sync_terminal_regress as base

ME, OTHER, BOARD = base.ME, base.OTHER, base.BOARD
kanban_home, svc, live = base.kanban_home, base.svc, base.live


def _regressed(live, kanban_home, svc, *, status, outcome, completed_at):
    tid, run_uid = base._running_card(live, kanban_home, svc)
    if outcome == "completed":
        run_id = live.execute("SELECT id FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0]
        with kbc.write_txn(live):
            kb._append_event(live, tid, "completed", {"summary": "landed"}, run_id=run_id)
    base._worker_ends(svc, tid, run_uid, status=status, outcome=outcome, completed_at=completed_at)
    base._round(kanban_home, svc)
    # The stale copy the hub sent at 08:27:50, as the Board stored it.
    svc.hub(OTHER).ops(BOARD, [{"client_op_id": "stale-copy", "op": "upsert", "table": "tasks", "uid": tid,
                                "hlc": base._hlc(3_000, OTHER),
                                "fields": {"status": "running", "completed_at": None, "current_run_uid": run_uid}}])
    return tid


def _clock_after_the_stale_write(live):
    """The owner heals later than the stale write landed (on the hub: minutes to hours later)."""
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET hlc_last = ?", (f"{int(time.time() * 1000) + 10_000:013d}-0000-{ME}",))


def _healed_events(live, tid):
    return live.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'status_healed'",
                        (tid,)).fetchone()[0]


@pytest.mark.parametrize("status,outcome,completed_at", [
    ("done", "completed", 1794472039),
    ("todo", "blocked", None),
])
def test_a_running_card_over_an_ended_run_is_moved_where_its_run_left_it(live, kanban_home, svc, status, outcome,
                                                                        completed_at):
    tid = _regressed(live, kanban_home, svc, status=status, outcome=outcome, completed_at=completed_at)
    _clock_after_the_stale_write(live)
    base._round(kanban_home, svc)  # pulls the regression, heals it, pushes the heal
    base._round(kanban_home, svc)

    assert base._service_task(svc, tid)["status"] == status
    assert base._service_task(svc, tid)["current_run_uid"] is None
    row = live.execute("SELECT status, current_run_id, completed_at, block_kind FROM tasks WHERE id = ?",
                       (tid,)).fetchone()
    assert (row["status"], row["current_run_id"]) == (status, None)
    assert (row["completed_at"] is not None) == (status == "done")
    if status == "done":
        assert row["block_kind"] is None
    assert _healed_events(live, tid) == 1


def test_a_heal_the_board_outvotes_is_reported_once_not_looped(live, kanban_home, svc):
    tid = _regressed(live, kanban_home, svc, status="done", outcome="completed", completed_at=1794472039)
    for _ in range(3):  # the stale write's clock is ahead of ours: every heal loses the field merge
        base._round(kanban_home, svc)
    assert _healed_events(live, tid) == 1


def test_a_running_card_with_its_run_open_is_left_alone(live, kanban_home, svc):
    tid, _run_uid = base._running_card(live, kanban_home, svc)
    assert heal_running_over_ended_run(live) == {}
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"
