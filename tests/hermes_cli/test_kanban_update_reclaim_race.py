"""Updater termination and the failure observer must not compete to close one run."""
import os
import signal
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from tests.hermes_cli import test_kanban_worker_pid_fingerprint as helpers

board = helpers.board
PID = 987654321


def running(board, monkeypatch):
    alive = {PID: True, os.getpid(): True}
    monkeypatch.setattr(dispatch._host_reboot, "current_boot", lambda: "boot")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(dispatch, "_pid_recycled", lambda *_: False)
    monkeypatch.setattr(dispatch, "_process_fingerprint", lambda *_: "boot|123")
    monkeypatch.setattr(dispatch, "_poll_worker_exit", lambda *_: not alive[PID])
    monkeypatch.setattr(dispatch, "_kill_fn", lambda hook: hook or (lambda pid, _: alive.update({pid: False})))
    monkeypatch.setattr(kb._requeue_custody, "for_requeue", lambda *_: {})
    tid = helpers._claimed_running(board, pid=PID, started_at="boot|123")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET consecutive_failures=1, claim_expires=9999999999 WHERE id=?", (tid,))
    return tid, alive


@pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern-loss"])
@pytest.mark.parametrize("observer_first", [True, False], ids=["crash-first", "requeue-first"])
def test_update_reclaim_never_charges_or_clears_strikes(board, monkeypatch, observer_first, modern):
    tid, alive = running(board, monkeypatch)
    if modern:
        from hermes_cli import kanban_failed_result as results
        monkeypatch.setattr(results, "reclaim_proof", lambda *_a, **_k: (
            {"result": {"loss_receipt": True, "reason": "EOFError"}}, None))
        monkeypatch.setattr(results, "result_exit_code", lambda *_: None)
    db_path = board.execute("PRAGMA database_list").fetchone()[2]
    observer = kbc.connect(Path(db_path))
    run_id = kb.get_task(board, tid).current_run_id

    def terminate(pid, sig):
        assert (pid, sig) == (PID, signal.SIGTERM)
        alive[pid] = False
        dispatch._record_worker_exit(pid, signal.SIGTERM)
        # Model the owner's EOF diagnostic without exempting ordinary EOFs.
        with kb.write_txn(observer):
            kb._append_event(observer, tid, "worker_lost", {"reason": "EOFError", "pid": pid}, run_id=run_id)
        if observer_first:
            dispatch.detect_crashed_workers(observer)
            assert kb.get_task(observer, tid).consecutive_failures == 1

    try:
        assert kb.reclaim_task(board, tid, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=terminate)
        dispatch.detect_crashed_workers(observer)
        task = kb.get_task(board, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert not {"crashed", "failure_finalized"} & {e.kind for e in kb.list_events(board, tid)}
        run = board.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["outcome"] == "reclaimed"
    finally:
        dispatch._recent_worker_exits.pop(PID, None)
        observer.close()


@pytest.mark.parametrize("reason", [None, "operator", CORE_UPDATE_REQUEUE_REASON + " other"])
def test_other_reclaims_do_not_hold_observer(board, monkeypatch, reason):
    from hermes_cli.kanban_update_reclaim_intent import fence, pending

    tid, _ = running(board, monkeypatch)
    row = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    with fence(board, tid, row, reason) as admitted:
        assert admitted
        assert not pending(board, row)


@pytest.mark.parametrize("field,value", [("current_run_id", -1), ("claim_lock", "other:12"),
                                        ("worker_pid", PID + 1), ("worker_started_at", "other|456")])
def test_intent_never_covers_replacement_identity(board, monkeypatch, field, value):
    from hermes_cli.kanban_update_reclaim_intent import fence, pending

    tid, _ = running(board, monkeypatch)
    row = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    with fence(board, tid, row, CORE_UPDATE_REQUEUE_REASON) as admitted:
        assert admitted and pending(board, row)
        changed = dict(row)
        changed[field] = value
        assert not pending(board, changed)
        with fence(board, tid, changed, CORE_UPDATE_REQUEUE_REASON) as stale:
            assert not stale
    assert pending(board, row)  # an unfinished command remains recoverable


def test_concurrent_reclaimer_cannot_steal_intent(board, monkeypatch):
    from hermes_cli.kanban_update_reclaim_intent import fence, pending

    tid, _ = running(board, monkeypatch)
    row = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    with fence(board, tid, row, CORE_UPDATE_REQUEUE_REASON) as admitted:
        assert admitted
        with fence(board, tid, row, CORE_UPDATE_REQUEUE_REASON) as duplicate:
            assert not duplicate
        assert pending(board, row)


def test_exception_keeps_durable_pause_recoverable(board, monkeypatch):
    from hermes_cli.kanban_update_reclaim_intent import fence, pending

    tid, alive = running(board, monkeypatch)
    row = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    with pytest.raises(RuntimeError, match="before termination"):
        with fence(board, tid, row, CORE_UPDATE_REQUEUE_REASON):
            raise RuntimeError("before termination")
    assert pending(board, row)
    alive[PID] = False
    dispatch._record_worker_exit(PID, signal.SIGTERM)
    try:
        dispatch.detect_crashed_workers(board)
        assert "failure_finalized" not in {e.kind for e in kb.list_events(board, tid)}
        assert kb.get_task(board, tid).consecutive_failures == 1
        assert kb.get_task(board, tid).status == "ready"
        assert not pending(board, row)
    finally:
        dispatch._recent_worker_exits.pop(PID, None)


def test_dead_reclaimer_does_not_leave_permanent_hold(board, monkeypatch):
    from hermes_cli.kanban_update_reclaim_intent import fence, pending

    tid, alive = running(board, monkeypatch)
    row = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    with fence(board, tid, row, CORE_UPDATE_REQUEUE_REASON) as admitted:
        assert admitted and pending(board, row)
        alive[os.getpid()] = False
        assert pending(board, row)
        dispatch.detect_crashed_workers(board)
        assert kb.get_task(board, tid).status == "ready"
        assert kb.get_task(board, tid).consecutive_failures == 1


def test_unreadable_reclaimer_identity_refuses_before_signal(board, monkeypatch):
    tid, _ = running(board, monkeypatch)
    monkeypatch.setattr(dispatch, "_process_fingerprint", lambda *_: None)
    assert not kb.reclaim_task(board, tid, reason=CORE_UPDATE_REQUEUE_REASON,
                               signal_fn=lambda *_: pytest.fail("must not signal"))


def test_sweep_before_intent_preparation_prevents_signal(board, monkeypatch):
    tid, alive = running(board, monkeypatch)
    original = kb._sync.refuse_foreign_reclaim

    def sweep_first(conn, task_id):
        alive[PID] = False
        dispatch._record_worker_exit(PID, signal.SIGTERM)
        dispatch.detect_crashed_workers(conn)
        return original(conn, task_id)

    monkeypatch.setattr(kb._sync, "refuse_foreign_reclaim", sweep_first)
    try:
        assert not kb.reclaim_task(board, tid, reason=CORE_UPDATE_REQUEUE_REASON,
                                   signal_fn=lambda *_: pytest.fail("stale selection must not signal"))
        assert "failure_finalized" in {e.kind for e in kb.list_events(board, tid)}
    finally:
        dispatch._recent_worker_exits.pop(PID, None)
