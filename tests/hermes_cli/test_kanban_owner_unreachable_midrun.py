"""A worker whose owner stops answering mid-turn is not a card failure. [daveey]

paintbot-tools/t_01a197166756bd53 run 207 (hermes-hub, 2026-11-13 20:06Z): the owner gateway, wedged
after an aborted core-update drain, never answered the worker's ``transcript.append``; the resend's
rediscovery timed out and the bound interpreter died with ``WorkerPersistenceError: owner_unavailable``.
The sweep booked it ``crashed`` and charged the card, unlike the sibling ``storage_unavailable``.
"""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_host_refusal as helpers

home = helpers.home

_TERMINAL = "Worker failed: WorkerPersistenceError: owner_unavailable"
# The turn's own failed result, recorded before the outer worker's exception envelope (run 207, event 10270).
_TURN_TEXT = ("⚠️ No reply: Hermes couldn't save this conversation, so it stopped rather than lose your "
              "messages.")


def _result(conn, tid, **changes):
    task = kb.get_task(conn, tid)
    payload = {"pid": os.getpid(), "claim_lock": task.claim_lock, "exit_code": 1, "last_output": _TERMINAL}
    payload.update(changes)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_result", payload, run_id=task.current_run_id)


def test_midrun_owner_unreachable_requeues_without_spending_budget(home):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries=1 WHERE id=?", (tid,))
        _result(conn, tid, last_output=_TURN_TEXT)
        _result(conn, tid)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        run = helpers._runs(conn, tid)[-1]
        assert run["outcome"] == "owner_unavailable"
        assert "turn interrupted" in run["error"] and "no turn ran" not in run["error"]
        assert "could not reach its profile owner" in run["error"]
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
        (notice,) = helpers._notices(conn)
        assert "could not reach its profile owner" in notice["body"]


@pytest.mark.parametrize("output", [
    "Worker failed: WorkerPersistenceError: owner_unavailable_extra",
    "Worker failed: RuntimeError: owner_unavailable",
    "quoted " + _TERMINAL,
    _TERMINAL + "\nAnother failure",
])
def test_inexact_envelope_still_counts(home, output):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, last_output=output)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert not helpers._notices(conn)


def test_later_card_fault_wins(home):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid)
        _result(conn, tid, last_output="Worker failed: ValueError: real card fault")
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


@pytest.mark.parametrize("field", ["claim_lock", "pid", "exit_code"])
def test_receipt_not_tied_to_the_bound_claim_counts(home, field):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, **{field: {"claim_lock": "other:1", "pid": 1, "exit_code": 2}[field]})
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_unreachable_owner_streak_still_reaches_the_breaker(home):
    from hermes_cli import kanban_owner_unavailable as ou
    with kbc.connect() as conn:
        tid = None
        for i in range(ou.NEUTRAL_STREAK_LIMIT + 1):
            tid, _ = helpers._admitted_run(conn, tid, submitter=73000 + i)
            _result(conn, tid)
            kbd.detect_crashed_workers(conn)
        runs = helpers._runs(conn, tid)
        assert [r["outcome"] for r in runs[:-1]] == ["owner_unavailable"] * ou.NEUTRAL_STREAK_LIMIT
        assert runs[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_storage_and_unreachable_proofs_are_distinct_kinds(home):
    from hermes_cli import kanban_host_refusal as hr
    from hermes_cli import kanban_owner_storage_refusal as storage
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid)
        task = kb.get_task(conn, tid)
        proof = storage.proven(conn, tid, task.current_run_id, task.claim_lock)
        assert proof == {"kind": hr.OWNER_UNREACHABLE, "phase": "midrun", "reason": "owner_unavailable"}


# Sibling path: the resend's redial reached the gateway, but send 2/2 still got no receipt or the
# owner dropped the link again (``runtime_session_store.exhausted_resend_code``).
_SILENT_RESEND = ["worker_receipt_timeout", "owner_link_closed"]


@pytest.mark.parametrize("code", _SILENT_RESEND)
def test_exhausted_resend_requeues_without_spending_budget(home, code):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries=1 WHERE id=?", (tid,))
        _result(conn, tid, last_output=_TURN_TEXT)
        _result(conn, tid, last_output=f"Worker failed: WorkerPersistenceError: {code}")
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        run = helpers._runs(conn, tid)[-1]
        assert run["outcome"] == "owner_unavailable"
        assert f"could not reach its profile owner to save the turn ({code}:" in run["error"]
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()


@pytest.mark.parametrize("code", _SILENT_RESEND)
def test_exhausted_resend_streak_still_reaches_the_breaker(home, code):
    from hermes_cli import kanban_owner_unavailable as ou
    with kbc.connect() as conn:
        tid = None
        for i in range(ou.NEUTRAL_STREAK_LIMIT + 1):
            tid, _ = helpers._admitted_run(conn, tid, submitter=74000 + i)
            _result(conn, tid, last_output=f"Worker failed: WorkerPersistenceError: {code}")
            kbd.detect_crashed_workers(conn)
        runs = helpers._runs(conn, tid)
        assert [r["outcome"] for r in runs[:-1]] == ["owner_unavailable"] * ou.NEUTRAL_STREAK_LIMIT
        assert runs[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1


@pytest.mark.parametrize("output", [
    "Worker failed: TimeoutError: worker_receipt_timeout",  # not the store's latched envelope
    "Worker failed: WorkerPersistenceError: worker_receipt_timeout_extra",
    # A close code that may answer this frame keeps its raw text and stays the card's.
    "Worker failed: WorkerPersistenceError: received 1009 (message too big); then sent 1009 (message too big)",
    "Worker failed: WorkerPersistenceError: owner_link_closed\nAnother failure",
])
def test_inexact_resend_envelope_still_counts(home, output):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, last_output=output)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert not helpers._notices(conn)
