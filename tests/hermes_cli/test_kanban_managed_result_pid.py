"""A managed Kanban run's lost-ACK recovery honours the exit its authority recorded.

``tasks.worker_pid`` is the dispatcher's spawned submitter, but the managed interpreter that ran
the turns lives under the profile authority and writes ``worker_result`` with ITS pid (the one
``worker_bound`` recorded). The dead-worker sweep must accept that result under the same claim,
so a quota wall (exit 75) requeues as rate-limited instead of booking a crash.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.mark.parametrize("bound_claim, expected", [("own", "rate_limited"), ("foreign", "crashed")])
def test_managed_exit_75_requeues_rate_limited_not_crashed(conn, bound_claim, expected):
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="managed", assignee="w")
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None
    submitter, interpreter = _dead_pid(), _dead_pid()
    assert submitter != interpreter
    kbd._set_worker_pid(conn, tid, submitter)
    claim = task.claim_lock if bound_claim == "own" else "someone-else"
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_bound", {"pid": interpreter, "claim_lock": claim, "started_at": "x"},
                         run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_result", {"pid": interpreter, "claim_lock": task.claim_lock,
                         "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE, "last_output": ""}, run_id=task.current_run_id)

    crashed = kbd.detect_crashed_workers(conn)

    outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]
    assert outcome == expected
    assert (tid in kbd.detect_crashed_workers._last_rate_limited) is (expected == "rate_limited")
    assert (tid in crashed) is (expected == "crashed")


@pytest.mark.parametrize("reply", ["Worker could not start: ModelRoutingError: no route", "", None])
@pytest.mark.parametrize("late_result", ["old_run", "wrong_pid", "wrong_claim"])
def test_crash_output_ignores_late_prior_run_result(conn, reply, late_result):
    import json

    tid = kb.create_task(conn, title="interleaved replies", assignee="w")
    first = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert first is not None
    old_pid = _dead_pid()
    kbd._set_worker_pid(conn, tid, old_pid)
    assert tid in kbd.detect_crashed_workers(conn)
    task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert task is not None and task.current_run_id != first.current_run_id
    submitter, interpreter = _dead_pid(), _dead_pid()
    kbd._set_worker_pid(conn, tid, submitter)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_bound", {
            "pid": interpreter, "claim_lock": task.claim_lock,
        }, run_id=task.current_run_id)
        if reply is not None:
            kb._append_event(conn, tid, "worker_result", {
                "pid": interpreter, "claim_lock": task.claim_lock,
                "exit_code": 1, "last_output": reply,
            }, run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_result", {
            "pid": interpreter if late_result == "wrong_claim" else old_pid,
            "claim_lock": "foreign" if late_result == "wrong_claim" else first.claim_lock,
            "exit_code": 0, "last_output": "OLD REPLY",
        }, run_id=first.current_run_id if late_result == "old_run" else task.current_run_id)
    # The old submitter can still flush to the shared task log after B's marker.
    with kbd._open_worker_log(task, None) as log:
        log.write(f"{reply}\nOLD REPLY\n[kanban-worker-exit] rc=1\n".encode())
    assert tid in kbd.detect_crashed_workers(conn)
    error = conn.execute("SELECT error FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]
    payload = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE run_id=? AND kind='crashed'",
        (task.current_run_id,),
    ).fetchone()[0])
    assert "OLD REPLY" not in error
    assert (reply or "") in error
    assert payload.get("worker_output", "") == (reply or "")
