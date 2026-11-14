"""A card the Board already reads ``running`` over its ended run is healed by its owner's sync round. [daveey]

Hub 2026-11-12: t_01a1ceaeb6ba593c (completed) and t_01a1ceb44e35f780 (blocked -> todo) were regressed on the Board
itself (v21 / v11), so the replica and the live file agreed and no divergence repair could see them."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid

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
              "scheduled": "scheduled", "continued": "continued"}


def _regressed(live, kanban_home, svc, *, status, outcome, completed_at, end_event=True, after=None,
               end_payload=None, stale=None):
    tid, run_uid = base._running_card(live, kanban_home, svc)
    if end_event and outcome in _END_EVENT:
        run_id = live.execute("SELECT id FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0]
        with kbc.write_txn(live):
            kb._append_event(live, tid, _END_EVENT[outcome], end_payload or {"summary": "run end"}, run_id=run_id)
            for kind, payload in after or ():
                kb._append_event(live, tid, kind, payload)
    base._worker_ends(svc, tid, run_uid, status=status, outcome=outcome, completed_at=completed_at)
    base._round(kanban_home, svc)
    # The stale copy the hub sent at 08:27:50, as the Board stored it.
    svc.hub(OTHER).ops(BOARD, [{"client_op_id": "stale-copy", "op": "upsert", "table": "tasks", "uid": tid,
                                "hlc": base._hlc(3_000, OTHER),
                                "fields": {"status": "running", "completed_at": None, "current_run_uid": run_uid,
                                           **(stale or {})}}])
    return tid


def _as_feed(live, sql, params=()):
    """Rewrite event rows as a feed apply would (no capture)."""
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute(sql, params)
        live.execute("UPDATE kanban_sync_state SET applying = 0")


def _uid_at(ms: int) -> str:
    h = f"{ms:012x}"
    return f"{h[:8]}-{h[8:12]}-7000-8000-000000000000"


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


@pytest.mark.parametrize("outcome,sweep", [
    ("blocked", "agent_block_escalated"),   # kanban_block_policy: blocked -> triage
    ("blocked", "ask_rechecked"),           # standing-ask recheck: blocked -> triage
    ("review_requested", "supervisor_escalated"),  # review -> triage
])
def test_a_sweep_that_moved_the_card_out_of_its_park_is_not_undone(live, kanban_home, svc, outcome, sweep):
    tid = _regressed(live, kanban_home, svc, status="triage", outcome=outcome, completed_at=None,
                     after=[(sweep, {"reason": "sweep"})])
    _heal_through_rounds(live, kanban_home, svc)
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "todo"


def test_a_scheduled_park_the_wake_invariant_moved_to_blocked_lands_blocked(live, kanban_home, svc):
    """``kanban_continuation._enforce_wake_invariant`` turns a wakeless human wait into ``blocked``."""
    tid = _regressed(live, kanban_home, svc, status="blocked", outcome="scheduled", completed_at=None,
                     after=[("blocked", {"reason": "human wait", "kind": "needs_input",
                                         "source_status": "scheduled"})])
    _heal_through_rounds(live, kanban_home, svc)
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "blocked"


def _status(live, tid):
    return live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


@pytest.mark.parametrize("landing,expected", [("scheduled", "scheduled"), ("ready", "todo")])
def test_a_continued_run_lands_where_its_own_event_put_it(live, kanban_home, svc, landing, expected):
    """``kanban_continue(resume_after_seconds=N)`` parks ``scheduled`` until its wake (PR1381 review probe 1); with
    no wait the card went straight back to ``ready``, which a reclaim's ``todo`` reaches as well."""
    tid = _regressed(live, kanban_home, svc, status=landing, outcome="continued", completed_at=None,
                     end_payload={"continuation": 1, "status": landing, "resume_status": "ready",
                                  "wake_at": int(time.time()) + 3600})
    _heal_through_rounds(live, kanban_home, svc)
    assert _status(live, tid) == expected


@pytest.mark.parametrize("left", [
    ("review_reopened", None),  # reopen_review_task writes no payload when it lands ``ready`` (review probe 2)
    ("triage_swept", {"reason": "sweep"}), ("gave_up", {"reason": "limit"}), ("reclaimed", {"reason": "stale"}),
])
def test_every_resume_lifecycle_move_counts_as_leaving_the_park(live, kanban_home, svc, left):
    """The list matches ``kanban_resume_exec._LIFECYCLE_KINDS``: each of these moves the card out of its park."""
    tid = _regressed(live, kanban_home, svc, status="ready", outcome="review_requested", completed_at=None,
                     after=[left])
    _heal_through_rounds(live, kanban_home, svc)
    assert _status(live, tid) == "todo"


@pytest.mark.parametrize("end_payload", [
    {"reason": "r", "kind": "transient", "source_status": "ready", "status": "triage"},  # block_task now records it
    {"reason": "r", "kind": "transient", "source_status": "ready", "rekind_reason": "continuation_exhausted"},
    {"reason": "r", "kind": "transient", "source_status": "ready", "rekind_reason": "goal_unachievable"},
])
def test_an_exhausted_budget_block_lands_triage_not_blocked(live, kanban_home, svc, end_payload):
    """``block_task`` writes a ``blocked`` event but lands ``triage`` on an exhausted budget (review probe 3)."""
    tid = _regressed(live, kanban_home, svc, status="triage", outcome="blocked", completed_at=None,
                     end_payload=end_payload)
    _heal_through_rounds(live, kanban_home, svc)
    assert _status(live, tid) == "triage"


def test_an_assignment_routed_block_lands_todo_behind_its_supervisor(live, kanban_home, svc):
    """``assignment_routing.route`` moves the card to ``todo`` behind a supervisor; ``blocked`` would strand it."""
    tid = _regressed(live, kanban_home, svc, status="todo", outcome="blocked", completed_at=None,
                     end_payload={"kind": "assignment", "reason": "r", "source_status": "ready", "status": "todo",
                                  "supervisor_task_id": "t_sup", "supervisor_profile": "supervisor"})
    _heal_through_rounds(live, kanban_home, svc)
    assert _status(live, tid) == "todo"


def test_a_metadata_only_block_event_is_not_a_move(live, kanban_home, svc):
    """``kanban_human_request_migration.classify`` re-files the ask on a ``triage`` card with a ``blocked`` event."""
    tid = _regressed(live, kanban_home, svc, status="triage", outcome="blocked", completed_at=None,
                     after=[("agent_block_escalated", {"reason": "sweep"}),
                            ("blocked", {"kind": "needs_input", "reason": "re-filed", "classified_in_place": True,
                                         "metadata_only": True, "human_request": None})])
    _heal_through_rounds(live, kanban_home, svc)
    assert _status(live, tid) == "todo"


def test_a_restored_ask_with_a_resolved_check_gets_its_probe_row(live, kanban_home, svc):
    ask = {"action": "Reply A or B. Unblock = build it; archive = drop it.", "why_human": "a toss-up",
           "interaction": "reply", "scope": "this card", "evidence": "two designs", "resolved_check": "false"}
    tid = _regressed(live, kanban_home, svc, status="blocked", outcome="blocked", completed_at=None,
                     end_payload={"reason": "pick", "kind": "needs_input", "human_request": ask},
                     stale={"block_kind": None, "human_request": None})
    live.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
    _heal_through_rounds(live, kanban_home, svc)
    assert live.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ? AND purpose = 'ask_resolved'",
                        (tid,)).fetchone()[0] == "false"


def test_a_non_v7_uid_does_not_reorder_same_second_events():
    from hermes_cli.kanban_sync_divergence import _event_order
    v4 = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    assert _event_order({"created_at": 100, "id": 5, "uid": v4}) == (100, 100_000, 5)
    assert _event_order({"created_at": 100, "id": 5, "uid": None}) == (100, 100_000, 5)
    v7 = "01a1cf86-8673-7c4f-883e-0f361585d8e4"
    own_second = 0x01a1cf868673 // 1000
    assert _event_order({"created_at": own_second, "id": 5, "uid": v7})[1] == 0x01a1cf868673


def test_a_backfilled_v7_uid_does_not_reorder_same_second_events():
    """``kanban_db_sync`` backfills uid-less rows with a v7 uid stamped at backfill time: a uid whose second is not
    the row's own is not event time, so the earlier-arrived row stays first even though its uid is later."""
    from hermes_cli.kanban_sync_divergence import _event_order
    backfilled = "01a1cf86-8673-7c4f-883e-0f361585d8e4"  # minted in a later second than the row's 100
    first = {"created_at": 100, "id": 5, "uid": backfilled}
    second = {"created_at": 100, "id": 6, "uid": None}
    assert _event_order(first) == (100, 100_000, 5)
    assert max((first, second), key=_event_order) is second


@pytest.mark.parametrize("uid_ms,key", [
    pytest.param(101_010, 101_010, id="straddle-10ms-into-next-second"),
    pytest.param(101_999, 101_999, id="last-straddle-millisecond"),
    pytest.param(102_000, 100_000, id="past-the-straddle-window"),
    pytest.param(99_999, 100_000, id="before-the-row-second"),
])
def test_event_order_straddle_window(uid_ms, key):
    """A uid minted just past created_at's second (the trigger runs after created_at is read) still orders within
    its own second, last; one a full second further out is a backfill and falls back to the id."""
    from hermes_cli.kanban_sync_divergence import _event_order
    uid = str(uuid.UUID(int=(uid_ms << 80) | (7 << 76) | (2 << 62)))
    assert _event_order({"created_at": 100, "id": 5, "uid": uid}) == (100, key, 5)


@pytest.mark.parametrize("unblock_later_ms,expected", [(5, "todo"), (-5, "blocked")])
def test_same_second_events_are_ordered_by_their_origin_clock_not_arrival(live, kanban_home, svc,
                                                                           unblock_later_ms, expected):
    """The Board may deliver an ``unblocked`` before the ``blocked`` it follows: same second, lower local id."""
    tid = _regressed(live, kanban_home, svc, status="todo", outcome="blocked", completed_at=None,
                     after=[("unblocked", None)])
    end_id, unblock_id = (live.execute("SELECT id FROM task_events WHERE task_id = ? AND kind = ?",
                                       (tid, k)).fetchone()[0] for k in ("blocked", "unblocked"))
    second = int(time.time()) + 60  # after every other event of the card
    ms = second * 1000 + 500
    # Arrival order: the unblock first (lower id), the park after it (higher id).
    _as_feed(live, "UPDATE task_events SET id = ?, created_at = ?, uid = ? WHERE id = ?",
             (unblock_id + 1000, second, _uid_at(ms), end_id))
    _as_feed(live, "UPDATE task_events SET created_at = ?, uid = ? WHERE id = ?",
             (second, _uid_at(ms + unblock_later_ms), unblock_id))
    _heal_through_rounds(live, kanban_home, svc)
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == expected


def test_a_malformed_event_payload_skips_that_row_not_the_heal(live, kanban_home, svc):
    tid = _regressed(live, kanban_home, svc, status="blocked", outcome="blocked", completed_at=None)
    _as_feed(live, "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'commented', ?, ?)",
             (tid, "{not json", int(time.time())))
    _clock_after_the_stale_write(live)
    result = base._round(kanban_home, svc)
    assert result.error is None
    assert live.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "blocked"


def _local_running_over_ended_run(live) -> str:
    tid = kb.create_task(live, title="local card", assignee="worker")
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        run_id = live.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                              "VALUES (?, 'worker', 'done', 'completed', ?, ?)",
                              (tid, int(time.time()) - 5, int(time.time()))).lastrowid
        live.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, tid))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    return tid


def test_a_card_whose_heal_raises_is_skipped_and_the_rest_heal(live, monkeypatch):
    from hermes_cli import kanban_sync_divergence as div
    first, second = _local_running_over_ended_run(live), _local_running_over_ended_run(live)
    real = div._parked_status

    def flaky(conn, tid, run):  # an outcome-``completed`` run with no ``completed`` event asks for the park
        if tid == first:
            raise sqlite3.OperationalError("malformed JSON")
        return real(conn, tid, run)

    monkeypatch.setattr(div, "_parked_status", flaky)
    assert heal_running_over_ended_run(live) == {second: "todo"}
    status = {t: live.execute("SELECT status FROM tasks WHERE id = ?", (t,)).fetchone()[0] for t in (first, second)}
    assert status == {first: "running", second: "todo"}


def test_a_restored_block_takes_its_kind_and_ask_from_the_park_event(live, kanban_home, svc):
    """The stale copy can carry the pre-park ``block_kind``/``human_request`` too (both ``NULL``)."""
    ask = {"action": "Reply A or B. Unblock = build it; archive = drop it.", "why_human": "a toss-up",
           "interaction": "reply", "scope": "this card", "evidence": "two designs"}
    tid = _regressed(live, kanban_home, svc, status="blocked", outcome="blocked", completed_at=None,
                     end_payload={"reason": "pick", "kind": "needs_input", "human_request": ask},
                     stale={"block_kind": None, "human_request": None})
    _heal_through_rounds(live, kanban_home, svc)
    row = live.execute("SELECT status, block_kind, human_request FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["status"], row["block_kind"]) == ("blocked", "needs_input")
    assert json.loads(row["human_request"]) == ask
    assert base._service_task(svc, tid)["block_kind"] == "needs_input"


def test_a_restored_schedule_drops_a_stale_ask(live, kanban_home, svc):
    """``schedule_task`` clears ``human_request``; a stale copy's ask must not ride back with the park."""
    tid = _regressed(live, kanban_home, svc, status="scheduled", outcome="scheduled", completed_at=None,
                     stale={"human_request": json.dumps({"action": "old ask"})})
    _heal_through_rounds(live, kanban_home, svc)
    row = live.execute("SELECT status, human_request FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["status"], row["human_request"]) == ("scheduled", None)


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
    assert sum("after one heal" in r.getMessage() and tid in r.getMessage() for r in caplog.records) == 1


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
