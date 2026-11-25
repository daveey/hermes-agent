"""Mid-turn owner storage refusals are not card failures. [daveey]"""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_host_refusal as helpers

home = helpers.home


def _result(conn, tid, **changes):
    task = kb.get_task(conn, tid)
    payload = {"pid": os.getpid(), "claim_lock": task.claim_lock, "exit_code": 1,
               "last_output": "Worker failed: WorkerPersistenceError: storage_unavailable"}
    payload.update(changes)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_result", payload, run_id=task.current_run_id)


def test_midrun_owner_refusal_requeues_without_spending_budget(home):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_retries=1 WHERE id=?", (tid,))
        _result(conn, tid, last_output="")  # the real turn records this before outer failure
        _result(conn, tid)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert helpers._runs(conn, tid)[-1]["outcome"] == "owner_unavailable"
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
        assert "no turn ran" not in helpers._runs(conn, tid)[-1]["error"]
        (notice,) = helpers._notices(conn)
        assert "no turn ran" not in notice["body"]


@pytest.mark.parametrize("error", ["outbox_full", "unsafe_outbox", "lost_receipt", "permission_denied",
                                  "stale_generation", "storage_unavailable_extra"])
def test_worker_local_errors_still_count(home, error):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, last_output=f"Worker failed: WorkerPersistenceError: {error}")
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert not helpers._notices(conn)


@pytest.mark.parametrize("output", ["Worker failed: ValueError: storage_unavailable", "card failed",
                                    "quoted Worker failed: WorkerPersistenceError: storage_unavailable",
                                    "Worker failed: WorkerPersistenceError: storage_unavailable\nAnother failure"])
def test_nonterminal_or_card_error_counts(home, output):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid, last_output=output)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


def test_latest_bound_result_wins(home):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid)
        _result(conn, tid, last_output="Worker failed: ValueError: real card fault")
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1


@pytest.mark.parametrize("change", ["no_conn", "no_task", "no_run", "no_claim", "no_bound",
                                   "bound_claim", "bound_pid", "no_result", "result_claim",
                                   "result_pid", "result_exit", "result_output"])
def test_proof_requires_exact_bound_terminal_receipt(home, change):
    from hermes_cli import kanban_owner_storage_refusal as storage
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid)
        task = kb.get_task(conn, tid)
        args = [conn, tid, task.current_run_id, task.claim_lock]
        if change.startswith("no_") and change not in ("no_bound", "no_result"):
            args[["no_conn", "no_task", "no_run", "no_claim"].index(change)] = None
        else:
            kind = "worker_bound" if "bound" in change else "worker_result"
            with kb.write_txn(conn):
                if change in ("no_bound", "no_result"):
                    conn.execute("DELETE FROM task_events WHERE task_id=? AND kind=?", (tid, kind))
                else:
                    field = {"claim": "claim_lock", "pid": "pid", "exit": "exit_code",
                             "output": "last_output"}[change.split("_")[1]]
                    conn.execute("UPDATE task_events SET payload=json_set(payload, ?, NULL) "
                                 "WHERE task_id=? AND kind=?", ("$." + field, tid, kind))
        assert storage.proven(*args) is None


@pytest.mark.parametrize("field", ["claim_lock", "pid"])
def test_missing_identity_on_both_receipts_cannot_agree(home, field):
    from hermes_cli import kanban_owner_storage_refusal as storage
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _result(conn, tid)
        task = kb.get_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=json_set(payload, ?, NULL) "
                         "WHERE task_id=? AND kind IN ('worker_bound','worker_result')", ("$." + field, tid))
        claim = None if field == "claim_lock" else task.claim_lock
        assert storage.proven(conn, tid, task.current_run_id, claim) is None


def test_midrun_notice_shared_and_streak_bounded(isolate_usage_accounting, home):
    from hermes_cli import kanban_owner_unavailable as ou
    with kbc.connect() as conn:
        tid = None
        for i in range(ou.NEUTRAL_STREAK_LIMIT + 1):
            tid, _ = helpers._admitted_run(conn, tid, submitter=72000 + i)
            _result(conn, tid)
            kbd.detect_crashed_workers(conn)
        runs = helpers._runs(conn, tid)
        assert [r["outcome"] for r in runs[:-1]] == ["owner_unavailable"] * ou.NEUTRAL_STREAK_LIMIT
        assert runs[-1]["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1
        (notice,) = helpers._notices(conn)
        assert not helpers._notice_comments(conn, notice["id"])
