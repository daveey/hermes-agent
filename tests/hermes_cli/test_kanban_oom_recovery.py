"""Typed OOM recovery: raised cap, MemAvailable admission, toolchain env, rate detector."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worker_scope as scope

GiB = 1024 ** 3


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(conn, tid, outcome, ended=None):
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) VALUES (?,?,?,?,?)",
        (tid, "crashed", 1, ended or int(time.time()), outcome))
    conn.commit()


ARGV = ["systemd-run", "--user", "--scope", "--property", f"MemoryMax={4 * GiB}", "--", "hermes"]


def test_oom_run_doubles_cap_and_caps_at_12gib(kanban_home, monkeypatch):
    monkeypatch.setattr(scope, "mem_available_bytes", lambda: 40 * GiB)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        assert scope.apply_oom_recovery(ARGV, tid) == ARGV          # no OOM yet
        _run(conn, tid, "oom_killed")
    assert f"MemoryMax={8 * GiB}" in scope.apply_oom_recovery(ARGV, tid)
    with kbc.connect() as conn:
        _run(conn, tid, "oom_killed")
        _run(conn, tid, "oom_killed")
    assert f"MemoryMax={12 * GiB}" in scope.apply_oom_recovery(ARGV, tid)
    assert scope.apply_oom_recovery(ARGV, tid)[-2:] == ["--", "hermes"]


def test_non_oom_latest_run_resets_streak(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        _run(conn, tid, "oom_killed")
        _run(conn, tid, "crashed")
    assert scope.consecutive_oom_runs(tid) == 0


def test_admission_refuses_raise_when_memavailable_low():
    assert scope.raised_memory_max(4 * GiB, 1, 6 * GiB) == 4 * GiB     # 6-4 reserve = 2 < base
    assert scope.raised_memory_max(4 * GiB, 1, None) == 4 * GiB
    assert scope.raised_memory_max(4 * GiB, 2, 10 * GiB) == 6 * GiB    # partial, bounded by avail-reserve
    assert scope.raised_memory_max(4 * GiB, 1, 23 * GiB) == 8 * GiB


def test_toolchain_env_defaults_do_not_override():
    env = {"VITEST_MAX_WORKERS": "6", "NODE_OPTIONS": "--foo"}
    scope.apply_toolchain_env(env)
    assert env["VITEST_MAX_WORKERS"] == "6"
    assert env["JEST_MAX_WORKERS"] == "2"
    assert env["NODE_OPTIONS"] == "--foo --max-old-space-size=3072"
    scope.apply_toolchain_env(env)
    assert env["NODE_OPTIONS"].count("max-old-space-size") == 1


def test_detector_opens_one_card_then_comments(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="victim", assignee="a")
        for _ in range(4):
            _run(conn, tid, "oom_killed")
    assert scope.note_oom_kill(tid) is None            # 5th kill: at threshold, not over
    with kbc.connect() as conn:
        _run(conn, tid, "oom_killed")
    alert = scope.note_oom_kill(tid)                   # 6th
    assert alert
    assert scope.note_oom_kill(tid) == alert           # same incident -> comment
    with kbc.connect() as conn:
        assert len(kb.list_comments(conn, alert)) == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key='worker-oom-rate-alert'").fetchone()[0] == 1
