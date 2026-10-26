"""Scope failures outrank a worker's misleading clean-exit trailer."""
import json
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli.test_kanban_worker_exit_trailer import _dead_worker_with_log


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("result,outcome", [("oom-kill", "oom_killed"), ("signal", "signaled")])
def test_scope_failure_overrides_clean_exit_and_reaches_retry_context(kanban_home, monkeypatch, result, outcome):
    calls = []
    def systemctl(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=(
            f"LoadState=loaded\nResult={result}\nMemoryMax=67108864\nMemoryPeak=67239936\n"
        ), stderr="")
    monkeypatch.setattr(subprocess, "run", systemctl)
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", dict)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="scope failure", assignee="a")
        for attempt in range(2):
            _dead_worker_with_log(conn, tid, 72001 + attempt, 0)
            run_id = kb.get_task(conn, tid).current_run_id
            kbd.detect_crashed_workers(conn)
            run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            assert run["outcome"] == outcome
            meta = kb._json_dict(run["metadata"])
            assert meta["scope_result"] == result
            assert meta["memory_max_bytes"] == 67108864
            assert meta["memory_peak_bytes"] == 67239936
            assert not meta.get("protocol_violation")
            assert kbd._protocol_violation_streak(conn, tid) == 0
            task = kb.get_task(conn, tid)
            assert task.consecutive_failures == attempt + 1
            assert task.status == ("ready" if attempt == 0 else "triage")
            if attempt:
                assert task.block_kind == "transient"
            assert "protocol violation" not in task.last_failure_error
            if result == "oom-kill":
                assert "OOM-killed at 64 MiB" in task.last_failure_error
                assert "reduce the footprint" in task.last_failure_error
                assert "OOM-killed at 64 MiB" in kb.build_worker_context(conn, tid)
        assert any("reset-failed" in argv for argv in calls)


def test_scope_retention_preserves_worker_arguments(monkeypatch):
    from types import SimpleNamespace
    from tools import process_registry as pr
    command = ["python", "worker.py", "--collect"]
    monkeypatch.setattr(pr, "restart_safe_gateway_child_argv", lambda *a, **kw: SimpleNamespace(
        argv=["systemd-run", "--user", "--scope", "--collect", "--", *command], mode="scope"))
    argv = kbd._restart_safe_worker_argv(SimpleNamespace(id="t_test", current_run_id=1), command)
    assert "--collect" not in argv[:argv.index("--")]
    assert argv[argv.index("--") + 1:] == command


@pytest.mark.parametrize("props", ["", "LoadState=not-found\nResult=oom-kill\n", "LoadState=loaded\nResult=success\n"])
def test_absent_or_successful_scope_keeps_clean_exit_protocol(monkeypatch, props):
    from hermes_cli import kanban_worker_scope as scope
    monkeypatch.setattr(scope, "_systemctl", lambda *a: subprocess.CompletedProcess(a, 0, props))
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: ("clean_exit", 0))
    monkeypatch.setattr(kb, "read_worker_log", lambda *a, **kw: "")
    dead = kbd._classify_dead_worker(1, None, task_id="t_fake", run_id=2)
    assert dead.protocol_violation


def test_snapshot_survives_scope_gc_but_cannot_cross_runs(monkeypatch):
    from hermes_cli import kanban_worker_scope as scope
    monkeypatch.setattr(scope, "_systemctl", lambda *a: None)
    log = scope.SNAPSHOT_PREFIX + json.dumps({
        "systemd_unit": "hermes-worker-kanban-t_fake-run-2.scope", "oom_kill": 1,
        "memory_max_bytes": 67108864, "memory_peak_bytes": 67108864})
    assert scope.read_failure("t_fake", 2, log)["memory_peak_bytes"] == 67108864
    assert scope.read_failure("t_fake", 3, log) is None
    assert scope.read_failure("t_other", 2, log) is None
    assert scope.read_failure("t_fake", 2, scope.SNAPSHOT_PREFIX + "[123]") is None
    assert scope.retain_failed_scope(["python", "--scope", "--collect", "--"]) == ["python", "--scope", "--collect", "--"]
