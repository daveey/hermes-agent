"""An operator resume survives a hermes-sync core-update requeue of the resumed run. [daveey]

Production (MacBook board, t_01a1d560c6d3bc3c, 2026-11-13): an operator resumed a card held
``active_pr``; its run 14054 was requeued 5 min later by the core updater's restart-safe
requeue and changed nothing, yet the guard held the card ``active_pr`` again because the
receipt named the run before 14054. A requeue is a pause (:mod:`kanban_core_update_requeue`),
so the receipt stays valid; the next real claim, a crash or any other reclaim still spends it.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_operator_resume as resume
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.hermes_cli import test_kanban_operator_resume as base

kanban_home, clock, _held, _guard = base.kanban_home, base.clock, base._held, base._guard


def _resumed(clock):
    with kbc.connect_closing() as conn:
        tid = _held(conn)  # R0: published the PR, then crashed
        clock[0] += 60
        resume.resume(conn, tid, reason="operator: continue the PR", actor="op")
    assert _guard(tid) is None
    return tid


def _run(clock, tid, reason, *, crashed=False):
    clock[0] += 60
    with kbc.connect_closing() as conn:
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        if crashed:
            run_id = kb.get_task(conn, tid).current_run_id
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "crashed", {"error": "boom"}, run_id=run_id)
        clock[0] += 60
        assert kb.reclaim_task(conn, tid, reason=reason, signal_fn=lambda *_: None)
        assert kb.get_task(conn, tid).status == "ready"


def test_core_update_requeue_keeps_the_receipt_until_the_next_real_run(kanban_home, clock):
    tid = _resumed(clock)
    _run(clock, tid, CORE_UPDATE_REQUEUE_REASON)  # R1: requeued, a pause
    assert _guard(tid) is None
    _run(clock, tid, CORE_UPDATE_REQUEUE_REASON)  # a second drain is still a pause
    assert _guard(tid) is None
    _run(clock, tid, "worker crashed")  # R2: the real continuation spends the receipt
    assert _guard(tid) == "active_pr"


def test_resume_recorded_while_newest_run_is_requeued_is_honoured(kanban_home, clock):
    with kbc.connect_closing() as conn:
        tid = _held(conn)
    _run(clock, tid, CORE_UPDATE_REQUEUE_REASON)  # the newest run is a requeue (the 08:07 resume)
    assert _guard(tid) == "active_pr"
    clock[0] += 60
    with kbc.connect_closing() as conn:
        payload = resume.resume(conn, tid, reason="operator: continue the PR", actor="op")
        newest = conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                              (tid,)).fetchone()
    assert newest["outcome"] == "reclaimed" and payload["after_run"] == resume._identity(newest)
    assert _guard(tid) is None
    _run(clock, tid, CORE_UPDATE_REQUEUE_REASON)
    assert _guard(tid) is None
    _run(clock, tid, "worker crashed")
    assert _guard(tid) == "active_pr"


@pytest.mark.parametrize("reason,crashed", [
    (CORE_UPDATE_REQUEUE_REASON, True),  # a requeue with crash evidence is no pause
    ("worker crashed", False),
    (CORE_UPDATE_REQUEUE_REASON + " (by hand)", False),
    (None, False),
], ids=["requeue_with_crash_event", "crash", "near_miss_reason", "no_reason"])
def test_crash_or_other_reclaim_spends_the_receipt(kanban_home, clock, reason, crashed):
    tid = _resumed(clock)
    _run(clock, tid, reason, crashed=crashed)
    assert _guard(tid) == "active_pr"


def _older_run_with_higher_id(tid, outcome="blocked"):
    """A replica row: id above the checkpoint run's, but started before it."""
    with kbc.connect_closing() as conn:
        first = conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id LIMIT 1", (tid,)).fetchone()
        cols = [c for c in first.keys() if c != "id"]
        row = {c: first[c] for c in cols}
        row.update(started_at=first["started_at"] - 100, ended_at=first["started_at"] - 50, outcome=outcome)
        if "uid" in row:
            row["uid"] = "01a1f925-0000-7000-8000-000000000001"
        with kb.write_txn(conn):
            conn.execute(f"INSERT INTO task_runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [row[c] for c in cols])


def test_receipt_survives_replica_run_ids_that_are_not_chronological(kanban_home, clock):
    tid = _resumed(clock)
    _run(clock, tid, CORE_UPDATE_REQUEUE_REASON)  # a pause after the checkpoint run
    _older_run_with_higher_id(tid)  # replica: older real run, highest id
    with kbc.connect_closing() as conn:
        by_id = conn.execute("SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                             (tid,)).fetchone()
        assert by_id["outcome"] == "blocked"  # the pre-fix walk would stop here
    assert _guard(tid) is None
    _run(clock, tid, "worker crashed")  # a genuinely newer run still spends it
    assert _guard(tid) == "active_pr"
