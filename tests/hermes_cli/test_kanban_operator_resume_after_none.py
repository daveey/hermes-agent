"""``resumed_after`` for a receipt naming no run, and its merge_authorized gate. [daveey]

A receipt recorded before the card ever ran carries ``after_run=None``. It stays valid while
no real run follows it (core-update requeues are pauses), and the first real run spends it.
Only a receipt that says ``merge_authorized: False`` counts: the resume never authorizes a
merge, so an event without that exact value (forged or foreign) grants nothing.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_operator_resume as resume
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.hermes_cli import test_kanban_operator_resume as base

kanban_home, clock = base.kanban_home, base.clock

_RECEIPT = {"via": "operator-resume", "scope": "one_continuation", "merge_authorized": False,
            "after_run": None}


def _receipt(conn, tid, clock, **overrides):
    clock[0] += 60
    payload = {k: v for k, v in {**_RECEIPT, **overrides}.items() if v is not ...}
    with kb.write_txn(conn):
        kb._append_event(conn, tid, resume.EVENT, payload)
    clock[0] += 60


def _run(conn, clock, tid, reason):
    assert kb.claim_task(conn, tid) is not None
    clock[0] += 60
    assert kb.reclaim_task(conn, tid, reason=reason, signal_fn=lambda *_: None)
    clock[0] += 60


def test_none_receipt_is_honoured_with_no_runs_or_only_requeues(kanban_home, clock):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="never ran", assignee="default")
        since = clock[0]
        _receipt(conn, tid, clock)
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
        assert resume.resumed_after(conn, tid, since) is True
        _run(conn, clock, tid, CORE_UPDATE_REQUEUE_REASON)  # a pause, not a run
        assert resume.resumed_after(conn, tid, since) is True
        _run(conn, clock, tid, CORE_UPDATE_REQUEUE_REASON)
        assert resume.resumed_after(conn, tid, since) is True


@pytest.mark.parametrize("requeued_first", [False, True], ids=["claim", "requeue_then_claim"])
def test_none_receipt_is_spent_by_the_first_real_run(kanban_home, clock, requeued_first):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="never ran", assignee="default")
        since = clock[0]
        _receipt(conn, tid, clock)
        if requeued_first:
            _run(conn, clock, tid, CORE_UPDATE_REQUEUE_REASON)
        assert kb.claim_task(conn, tid) is not None  # a live claimed run already spends it
        assert resume.resumed_after(conn, tid, since) is False
        clock[0] += 60
        assert kb.reclaim_task(conn, tid, reason="worker crashed", signal_fn=lambda *_: None)
        assert resume.resumed_after(conn, tid, since) is False


@pytest.mark.parametrize("merge_authorized", [True, ..., None, 0, "false"],
                         ids=["true", "missing", "null", "zero", "string_false"])
def test_receipt_without_merge_authorized_false_grants_nothing(kanban_home, clock, merge_authorized):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="never ran", assignee="default")
        since = clock[0]
        _receipt(conn, tid, clock, merge_authorized=merge_authorized)
        assert resume.resumed_after(conn, tid, since) is False
        _receipt(conn, tid, clock)  # the same receipt saying False is honoured
        assert resume.resumed_after(conn, tid, since) is True
