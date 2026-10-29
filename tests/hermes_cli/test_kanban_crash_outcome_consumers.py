"""Classified scope deaths retain crash recovery policy, not log-text diagnoses."""
import contextlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_worker_scope as scope
from hermes_cli.kanban_continuation import consecutive_continuations
from hermes_cli.kanban_diagnostics import _rule_repeated_crashes, _rule_repeated_failures
from hermes_cli.kanban_shared_blockers import blocker_states
from tests.hermes_cli.test_kanban_worker_exit_trailer import _dead_worker_with_log

pytest_plugins = ["tests.hermes_cli.test_kanban_model_tier"]


@pytest.mark.parametrize("result,outcome", [("oom-kill", "oom_killed"), ("signal", "signaled"), (None, "crashed")])
@pytest.mark.parametrize("output", ["claude auth status --text", "rate limit exceeded"])
def test_classification_to_guard_and_strong_retry(board, monkeypatch, result, outcome, output):
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(scope, "read_failure", lambda *a: {
        "scope_result": result, "systemd_unit": "test.scope",
        "memory_max_bytes": 67108864, "memory_peak_bytes": 67239936,
    } if result else None)
    monkeypatch.setattr(scope, "release_failure", lambda *a: None)
    monkeypatch.setattr(dispatch, "_worker_final_output", lambda *a, **kw: output)
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 3600)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine fix", assignee="default")
        _dead_worker_with_log(conn, tid, 72001, 1)
        run_id = kb.get_task(conn, tid).current_run_id
        dispatch.detect_crashed_workers(conn)
        run = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["outcome"] == outcome
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        error = task.last_failure_error
        assert output in error
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert kb.get_task(conn, tid).last_failure_error == error
        model, provider, _ = tier.resolve_model_tier(conn, task, lane="ready", cfg={"default_model": "class:standard"})
        assert (model, provider) == ("class:deep", "auto")


def _end(conn, tid, outcome, metadata=None):
    kb.claim_task(conn, tid)
    conn.execute("UPDATE task_runs SET status='failed', outcome=?, ended_at=?, metadata=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                 (outcome, int(time.time()), json.dumps(metadata or {}), tid))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    conn.commit()


@pytest.mark.parametrize("outcome", ["crashed", "oom_killed", "signaled"])
def test_death_neither_clears_external_blocker_nor_resets_continuation_budget(board, outcome):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine fix", assignee="default")
        _end(conn, tid, "continued", {"blocker_id": "external-ci"})
        _end(conn, tid, outcome)
        assert consecutive_continuations(conn, tid) == 1
        state = blocker_states(conn)["external-ci"]
        assert tid in state.behind
        assert state.cleared_by is None


@pytest.mark.parametrize("outcome", ["crashed", "oom_killed", "signaled"])
def test_killed_workers_retain_crash_diagnostics(outcome):
    task = {"id": "t_test", "status": "ready", "consecutive_failures": 2}
    runs = [{"id": i, "outcome": outcome, "error": "worker died"} for i in (1, 2)]
    assert _rule_repeated_crashes(task, [], runs, 100, {})
    task["consecutive_failures"] = 3
    result = _rule_repeated_failures(task, [], runs, 100, {})
    assert result[0].data["most_recent_outcome"] == outcome


def test_oom_reclaim_does_not_take_a_second_write_txn(board, monkeypatch):
    """A legacy inflight row makes the next connect's backfill take a write txn.

    ``detect_crashed_workers`` already holds one. ``note_oom_kill`` used to open
    a second connection for the OOM counter; that connect re-ran
    ``_backfill_legacy_inflight_runs`` and blocked in ``BEGIN IMMEDIATE`` for
    the busy timeout (the shard-0 hang). The counter must stay on the caller's
    connection, and an over-threshold reclaim must still open the alert card.
    """
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(scope, "read_failure", lambda *a: {
        "scope_result": "oom-kill", "systemd_unit": "test.scope",
        "memory_max_bytes": 67108864, "memory_peak_bytes": 67239936,
    })
    monkeypatch.setattr(scope, "release_failure", lambda *a: None)
    holder: dict = {"conn": None}
    crossed = []
    real_write_txn = kbc.write_txn

    @contextlib.contextmanager
    def guarding_write_txn(conn, *, allow_nested=False):
        # BEGIN IMMEDIATE on any other connection while reclaim holds its write
        # txn is the deadlock (busy_timeout, then retries). Fail it immediately
        # so the regression asserts instead of stalling the suite.
        if holder["conn"] is not None and conn is not holder["conn"] and holder["conn"].in_transaction:
            crossed.append(conn)
            raise sqlite3.OperationalError("database is locked")
        with real_write_txn(conn, allow_nested=allow_nested) as yielded:
            yield yielded

    monkeypatch.setattr(kbc, "write_txn", guarding_write_txn)

    with kbc.connect() as conn:
        legacy = kb.create_task(conn, title="legacy inflight", assignee="default")
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=NULL, worker_pid=NULL, "
            "claim_lock=NULL, started_at=? WHERE id=?",
            (int(time.time()) - 300, legacy),
        )
        for _ in range(scope.OOM_ALERT_THRESHOLD):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                "VALUES (?, 'crashed', 'oom_killed', ?, ?)",
                (legacy, int(time.time()) - 10, int(time.time()) - 10),
            )
        victim = kb.create_task(conn, title="oom victim", assignee="default")
        _dead_worker_with_log(conn, victim, 72001, 1)
        conn.commit()
        # Force note_oom_kill's connect through the migration/backfill path.
        db_file = kbc._main_db_file(conn)
        assert db_file
        kbc._INITIALIZED_PATHS.discard(str(Path(db_file).resolve()))
        holder["conn"] = conn
        dispatch.detect_crashed_workers(conn)
        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? AND outcome IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (victim,),
        ).fetchone()
        alert = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key='worker-oom-rate-alert' "
            "AND status NOT IN ('done', 'archived')"
        ).fetchone()
    assert not crossed, "note_oom_kill opened a second write txn inside reclaim"
    assert run["outcome"] == "oom_killed"
    assert alert is not None
