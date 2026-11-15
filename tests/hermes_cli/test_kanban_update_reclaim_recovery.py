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
