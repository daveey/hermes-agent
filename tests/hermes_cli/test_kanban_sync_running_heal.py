"""A card the Board already reads ``running`` over its ended run is healed by its owner's sync round. [daveey]

Hub 2026-11-12: t_01a1ceaeb6ba593c (completed) and t_01a1ceb44e35f780 (blocked -> todo) were regressed on the Board
itself (v21 / v11), so the replica and the live file agreed and no divergence repair could see them."""
from __future__ import annotations

import logging
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_sync_divergence import heal_running_over_ended_run
from tests.hermes_cli import test_kanban_sync_terminal_regress as base

ME, OTHER, BOARD = base.ME, base.OTHER, base.BOARD
kanban_home, svc, live = base.kanban_home, base.svc, base.live

# The event each run end writes in its own transaction.
_END_EVENT = {"completed": "completed", "blocked": "blocked", "review_requested": "review_requested",
              "scheduled": "scheduled"}


def _regressed(live, kanban_home, svc, *, status, outcome, completed_at, end_event=True, after=None):
    tid, run_uid = base._running_card(live, kanban_home, svc)
    if end_event and outcome in _END_EVENT:
        run_id = live.execute("SELECT id FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0]
        with kbc.write_txn(live):
            kb._append_event(live, tid, _END_EVENT[outcome], {"summary": "run end"}, run_id=run_id)
            for kind, payload in after or ():
                kb._append_event(live, tid, kind, payload)
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


def _heal_through_rounds(live, kanban_home, svc):
    _clock_after_the_stale_write(live)
    base._round(kanban_home, svc)  # pulls the regression, heals it, pushes the heal
    base._round(kanban_home, svc)


@pytest.mark.parametrize("status,outcome,completed_at", [
    ("done", "completed", 1794472039),
    ("todo", "blocked", None),
])
def test_a_running_card_over_an_ended_run_is_moved_where_its_run_left_it(live, kanban_home, svc, status, outcome,
                                                                        completed_at):
    # The blocked case has no ``blocked`` event: t_01a1ceb44e35f780's run ended blocked, its card read todo.
    tid = _regressed(live, kanban_home, svc, status=status, outcome=outcome, completed_at=completed_at,
                     end_event=outcome == "completed")
    _heal_through_rounds(live, kanban_home, svc)

    assert base._service_task(svc, tid)["status"] == status
    assert base._service_task(svc, tid)["current_run_uid"] is None
    row = live.execute("SELECT status, current_run_id, completed_at, block_kind FROM tasks WHERE id = ?",
                       (tid,)).fetchone()
    assert (row["status"], row["current_run_id"]) == (status, None)
    assert (row["completed_at"] is not None) == (status == "done")
    if status == "done":
        assert row["block_kind"] is None
    assert _healed_events(live, tid) == 1


@pytest.mark.parametrize("status,outcome", [
    ("blocked", "blocked"),
    ("review", "review_requested"),
    ("scheduled", "scheduled"),
])
def test_a_run_that_parked_the_card_puts_it_back_in_that_park_not_todo(live, kanban_home, svc, status, outcome):
    """Plain ``todo`` would re-dispatch a card that was waiting on a human, a reviewer or a clock."""
    tid = _regressed(live, kanban_home, svc, status=status, outcome=outcome, completed_at=None)
    _heal_through_rounds(live, kanban_home, svc)

    assert live.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[:] \
        == (status, None)
    assert base._service_task(svc, tid)["status"] == status


@pytest.mark.parametrize("left", [("unblocked", None), ("condition_met", {"status": "ready"})])
def test_a_park_the_card_has_since_left_falls_back_to_todo(live, kanban_home, svc, left):
    tid = _regressed(live, kanban_home, svc, status="todo", outcome="blocked", completed_at=None, after=[left])
    _heal_through_rounds(live, kanban_home, svc)
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "todo"


def test_the_heal_event_names_its_run_so_the_board_gets_the_run_uid(live, kanban_home, svc):
    tid = _regressed(live, kanban_home, svc, status="done", outcome="completed", completed_at=1794472039)
    _heal_through_rounds(live, kanban_home, svc)
    run_uid = base._service_task(svc, tid).get("current_run_uid")
    assert run_uid is None
    local = live.execute("SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'status_healed'",
                         (tid,)).fetchone()
    assert local["run_id"] is not None
    assert '"run_id"' not in (local["payload"] or "")  # a local id means nothing on another gateway
    events = [e for e in svc.board(BOARD)["rows"]["task_events"].values()
              if e.get("task_id") == tid and e.get("kind") == "status_healed"]
    assert len(events) == 1
    assert events[0]["run_uid"] == live.execute("SELECT uid FROM task_runs WHERE id = ?",
                                                (local["run_id"],)).fetchone()[0]


def test_a_heal_the_board_outvotes_is_reported_once_not_looped(live, kanban_home, svc, caplog):
    tid = _regressed(live, kanban_home, svc, status="done", outcome="completed", completed_at=1794472039)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_sync_divergence"):
        for _ in range(3):  # the stale write's clock is ahead of ours: every heal loses the field merge
            base._round(kanban_home, svc)
    assert _healed_events(live, tid) == 1
    assert any("after one heal" in r.getMessage() and tid in r.getMessage() for r in caplog.records)


def test_a_running_card_with_its_run_open_is_left_alone(live, kanban_home, svc):
    tid, _run_uid = base._running_card(live, kanban_home, svc)
    assert heal_running_over_ended_run(live) == {}
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"


@pytest.mark.parametrize("column,holder", [("claim_lock", "hermes-worker-4:123"), ("lease_holder", OTHER)])
def test_a_held_claim_or_lease_keeps_the_card_running_over_an_ended_run(live, kanban_home, svc, column, holder):
    """A live holder still owns the card: the heal never takes it away, whatever its run row says."""
    tid, run_uid = base._running_card(live, kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("UPDATE task_runs SET ended_at = ?, outcome = 'completed' WHERE uid = ?",
                     (int(time.time()), run_uid))
        live.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (holder, tid))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    assert heal_running_over_ended_run(live) == {}
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"
    assert _healed_events(live, tid) == 0


def test_divergence_repair_never_adopts_a_terminal_replica_over_a_live_claim(live, kanban_home, svc):
    """Replica ``done`` on the same run, live non-terminal with a local worker's ``claim_lock``: not stale."""
    tid, run_uid = base._running_card(live, kanban_home, svc)
    base._worker_ends(svc, tid, run_uid, status="done", outcome="completed", completed_at=1794472039)
    base._live_missed_the_task_fields(live, kanban_home, svc, tid, run_uid)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("UPDATE tasks SET status = 'ready', claim_lock = 'hermes-worker-4:123' WHERE id = ?", (tid,))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        ksc.repair_divergence(live, shadow, ME)
    finally:
        shadow.close()
    row = live.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["status"], row["claim_lock"]) == ("ready", "hermes-worker-4:123")
