"""Spending follows whether a turn ran, not the sweep's failure-budget outcome."""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_owner_unavailable as ou
from hermes_cli import kanban_spend_budget as ksb
from tests.hermes_cli import test_kanban_host_refusal as helpers

home = helpers.home
_TERMINAL = "Worker failed: WorkerPersistenceError: owner_unavailable"


def _backdate(conn, tid, seconds=3600):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = started_at - ? WHERE task_id = ? AND ended_at IS NULL",
                     (seconds, tid))
        conn.execute("UPDATE tasks SET started_at = started_at - ? WHERE id = ?", (seconds, tid))


def _result(conn, tid, **changes):
    task = kb.get_task(conn, tid)
    payload = {"pid": os.getpid(), "claim_lock": task.claim_lock, "exit_code": 1, "last_output": _TERMINAL}
    payload.update(changes)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_result", payload, run_id=task.current_run_id)


def _last(conn, tid):
    return conn.execute("SELECT outcome, error, metadata, ended_at - started_at AS secs FROM task_runs "
                        "WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()


@pytest.mark.parametrize("usage", [None, {"cache_read_tokens": 5_000_000, "input_tokens": 9}])
def test_midrun_owner_loss_is_a_spending_run(home, usage):
    with kbc.connect() as conn:
        tid, _ = helpers._admitted_run(conn)
        _backdate(conn, tid)
        if usage is not None:
            _result(conn, tid, last_output="turn text", usage=usage)
        _result(conn, tid)
        kbd.detect_crashed_workers(conn)
        run = _last(conn, tid)
        assert run["outcome"] == "owner_unavailable" and "turn interrupted" in run["error"]
        assert run["secs"] >= 3600
        spent = ksb.spend(conn, tid)
        assert (spent.runs, spent.excluded) == (1, 0)
        assert spent.reported <= spent.runs
        assert spent.tokens["cache_read_tokens"] == (5_000_000 if usage else 0)
        assert spent.reported == (1 if usage else 0)


def test_admitted_run_lost_with_exit_69_is_a_spending_run(home, monkeypatch):
    """The owner admitted the run but the submitter never saw the answer."""
    from hermes_cli.kanban_db_dispatch_exit_code import exit_code_kind
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: exit_code_kind(ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="goal card", assignee="default")
        kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w71001")
        run_id = kb.get_task(conn, tid).current_run_id
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=71001 WHERE id=?", (tid,))
            kb._append_event(conn, tid, "owner_admitted", {"db": "/nowhere"}, run_id=run_id)
        _backdate(conn, tid)
        kbd.detect_crashed_workers(conn)
        run = _last(conn, tid)
        assert run["outcome"] == "crashed" and "the owner had admitted this run" in run["error"]
        spent = ksb.spend(conn, tid)
        assert (spent.runs, spent.excluded) == (1, 0)


def test_unadmitted_69_streak_is_not_spending(home, monkeypatch):
    from hermes_cli.kanban_db_dispatch_exit_code import exit_code_kind
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: exit_code_kind(ou.KANBAN_OWNER_UNAVAILABLE_EXIT_CODE))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="goal card", assignee="default")
        outcomes = []
        for n in range(6):
            kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w{71001 + n}")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (71001 + n, tid))
            _backdate(conn, tid, 120)
            kbd.detect_crashed_workers(conn)
            outcomes.append(_last(conn, tid)["outcome"])
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert outcomes == ["owner_unavailable"] * 5 + ["crashed"]
        spent = ksb.spend(conn, tid)
        assert (spent.runs, spent.excluded) == (0, 6)
