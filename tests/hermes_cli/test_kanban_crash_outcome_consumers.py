"""Classified scope deaths retain crash recovery policy, not log-text diagnoses."""
import json
import time

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
