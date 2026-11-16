"""An accepted update pause survives the terminating CLI and competing reclaim writers."""
import signal

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON
from hermes_cli import kanban_update_reclaim_intent as intent
from tests.hermes_cli import test_kanban_update_reclaim_race as race

board = race.board


def test_post_signal_custody_exception_is_resumed_neutrally(board, monkeypatch):
    tid, alive = race.running(board, monkeypatch)
    settled = []

    def custody(*_):
        settled.append(True)
        if len(settled) == 1:
            raise RuntimeError("custody interrupted")
        return {}

    monkeypatch.setattr(kb._requeue_custody, "for_requeue", custody)
    with pytest.raises(RuntimeError, match="custody interrupted"):
        kb.reclaim_task(board, tid, reason=CORE_UPDATE_REQUEUE_REASON)
    assert not alive[race.PID]
    assert intent.task_pending(board, tid)
    dispatch.detect_crashed_workers(board)
    assert len(settled) == 2
    assert kb.get_task(board, tid).status == "ready"
    assert kb.get_task(board, tid).consecutive_failures == 1
    assert "failure_finalized" not in {e.kind for e in kb.list_events(board, tid)}
    assert not intent.task_pending(board, tid)


@pytest.mark.parametrize("writer", ["lease", "timeout"])
def test_competing_reclaim_cannot_charge_updater_termination(board, monkeypatch, writer):
    tid, alive = race.running(board, monkeypatch)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET claim_expires=1, max_runtime_seconds=1 WHERE id=?", (tid,))

    def terminate(pid, sig):
        assert sig == signal.SIGTERM
        alive[pid] = False
        if writer == "lease":
            assert kb.release_stale_claims(board) == 0
        else:
            assert dispatch.enforce_max_runtime(board) == []

    assert kb.reclaim_task(board, tid, reason=CORE_UPDATE_REQUEUE_REASON, signal_fn=terminate)
    assert kb.get_task(board, tid).consecutive_failures == 1
    assert "failure_finalized" not in {e.kind for e in kb.list_events(board, tid)}


def test_no_intent_does_not_exempt_real_sigterm(board, monkeypatch):
    tid, alive = race.running(board, monkeypatch)
    alive[race.PID] = False
    dispatch._record_worker_exit(race.PID, signal.SIGTERM)
    try:
        dispatch.detect_crashed_workers(board)
        assert kb.get_task(board, tid).consecutive_failures == 2
        assert "failure_finalized" in {e.kind for e in kb.list_events(board, tid)}
    finally:
        dispatch._recent_worker_exits.pop(race.PID, None)


def test_resumed_pause_keeps_explicit_operator_attribution(board, monkeypatch):
    tid, _alive = race.running(board, monkeypatch)
    target = "0123456789abcdef0123456789abcdef01234567"
    settled = []

    def custody(*_):
        settled.append(True)
        if len(settled) == 1:
            raise RuntimeError("custody interrupted")
        return {}

    monkeypatch.setattr(kb._requeue_custody, "for_requeue", custody)
    with pytest.raises(RuntimeError, match="custody interrupted"):
        kb.reclaim_task(board, tid, reason=CORE_UPDATE_REQUEUE_REASON,
                        urgency_source="explicit_operator", interrupt_target=target)
    assert intent.task_pending(board, tid)
    dispatch.detect_crashed_workers(board)
    reclaimed = [e.payload for e in kb.list_events(board, tid) if e.kind == "reclaimed"]
    assert len(reclaimed) == 1
    assert reclaimed[0].get("urgency_source") == "explicit_operator"
    assert reclaimed[0].get("interrupt_target") == target
    assert kb.get_task(board, tid).status == "ready"
    assert not intent.task_pending(board, tid)


@pytest.mark.parametrize("then", ["finished", "replaced"])
def test_stale_recovery_never_touches_a_replacement_attempt(board, monkeypatch, then):
    """Observer A matches an abandoned pause, then stalls; observer B finishes it and
    the dispatcher starts a new attempt (optionally fenced by a new pause). A must
    become a no-op: no signal, no reclaimed receipt and no close for the new run."""
    tid, alive = race.running(board, monkeypatch)
    row = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    with intent.fence(board, tid, row, CORE_UPDATE_REQUEUE_REASON):
        pass  # the updater died after publishing: an abandoned, exact-run pause
    assert intent.task_pending(board, tid)
    original = intent._matching
    replacement = {}

    def stall_after_match(saved, current):
        matched = original(saved, current)
        if replacement or saved is None or saved["current_run_id"] != row["current_run_id"]:
            return matched
        replacement["run"] = None  # fire once: everything below happens while A stalls
        intent.recover_and_drain(board)  # observer B completes the same pause
        assert kb.get_task(board, tid).status == "ready"
        assert kb.claim_task(board, tid)
        alive[race.PID + 1] = True
        dispatch._set_worker_pid(board, tid, race.PID + 1)
        replacement["run"] = kb.get_task(board, tid).current_run_id
        if then == "replaced":
            fresh = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            with intent.fence(board, tid, fresh, CORE_UPDATE_REQUEUE_REASON):
                pass
        return matched

    monkeypatch.setattr(intent, "_matching", stall_after_match)
    intent.recover_and_drain(board)  # observer A resumes after its matching read
    task = kb.get_task(board, tid)
    new_run = replacement["run"]
    assert new_run is not None and new_run != row["current_run_id"]
    assert alive[race.PID + 1], f"replacement worker was signalled by stale recovery; run {new_run} {task.status}"
    assert task.status == "running" and task.current_run_id == new_run
    assert not [e for e in kb.list_events(board, tid) if e.kind == "reclaimed" and e.run_id == new_run]
    # A replacement's own pause stays durable for a later, legitimate recovery.
    assert intent.task_pending(board, tid) is (then == "replaced")
