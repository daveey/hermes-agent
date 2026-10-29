"""A task workspace file named like a stdlib module must not crash-loop its card. [daveey]

Hub cards t_01a185a36329ca4e and t_01a18555704bff20 wrote a helper ``inspect.py`` into their
workspace. The worker starts in the workspace as ``python -m hermes_cli.main``, ``-m`` put the
cwd first on ``sys.path``, ``dataclasses`` imported the helper instead of the stdlib
``inspect``, and every respawn died in ~15 s with ``exited with code 1`` until the breaker gave
up. Contracts:

* the worker argv the dispatcher really builds starts from such a workspace (``--version``
  rc 0), i.e. the cwd is not on the worker's import path;
* a pre-heartbeat crash with a stdlib-named file in the workspace names that file in the
  crash error; with a traceback frame from it the card is blocked at once (``assignment``),
  never retried; a run that heartbeat, or a workspace with no such file, is booked as before.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_workspace_shadow as shadow

REPO_ROOT = Path(__file__).resolve().parents[2]
_FAKE_INSPECT = "import sys\nprint('FAKE inspect from the workspace')\nsys.exit(1)\n"


def test_worker_argv_starts_from_workspace_holding_inspect_py(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "inspect.py").write_text(_FAKE_INSPECT, encoding="utf-8")
    monkeypatch.delenv("HERMES_BIN", raising=False)
    argv = kbd._resolve_hermes_argv()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run([*argv, "--version"], cwd=workspace, env=env, capture_output=True, text=True,
                       timeout=60, check=False)
    assert "FAKE inspect" not in r.stdout + r.stderr, (argv, r.stdout[-400:], r.stderr[-400:])
    assert r.returncode == 0, (argv, r.returncode, r.stdout[-400:], r.stderr[-400:])


def test_stdlib_shadows_lists_only_top_level_stdlib_names(tmp_path):
    (tmp_path / "inspect.py").write_text("", encoding="utf-8")
    (tmp_path / "json.py").write_text("", encoding="utf-8")
    (tmp_path / "probe.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "os.py").write_text("", encoding="utf-8")
    assert shadow.stdlib_shadows(tmp_path) == ["inspect.py", "json.py"]
    assert shadow.stdlib_shadows(tmp_path / "missing") == []


# dispatcher booking ------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _dead_worker(conn, *, log: str, files: dict[str, str], heartbeat: bool = False, pid: int = 73000):
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="helper script", assignee="default", max_retries=5)
    kb.claim_task(conn, tid, claimer=f"{host}:w0")
    workspace = kb.workspaces_root() / tid
    workspace.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (workspace / name).write_text(text, encoding="utf-8")
    conn.execute("UPDATE tasks SET worker_pid=?, workspace_path=? WHERE id=?", (pid, str(workspace), tid))
    conn.commit()
    if heartbeat:
        kbd.heartbeat_worker(conn, tid)
    path = kb.worker_log_path(tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(log.replace("{ws}", str(workspace)), encoding="utf-8")
    kbd._record_worker_exit(pid, 1 << 8)
    return tid, workspace


_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/usr/lib/python3.14/dataclasses.py", line 5, in <module>\n'
    "    import inspect\n"
    '  File "{ws}/inspect.py", line 3, in <module>\n'
    "    raise SystemExit(1)\n"
)


def test_traceback_from_workspace_inspect_py_blocks_at_once_and_names_it(board):
    with kbc.connect() as conn:
        tid, _ = _dead_worker(conn, log=_TRACEBACK, files={"inspect.py": _FAKE_INSPECT})
        assert tid in kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "triage", task.status
        assert task.block_kind == "assignment", task.block_kind
        assert task.consecutive_failures == 1
        assert "inspect.py shadows stdlib module inspect" in task.last_failure_error
        gave_up = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
        assert json.loads(gave_up["payload"])["stdlib_shadow"] is True
        crashed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='crashed'", (tid,)).fetchone()
        assert json.loads(crashed["payload"])["workspace_stdlib_shadows"] == ["inspect.py"]


def test_silent_shadow_exit_names_file_but_keeps_retry(board):
    """The helper ran and exited 1 with its own output (no traceback): hint, normal retry budget."""
    with kbc.connect() as conn:
        tid, _ = _dead_worker(conn, log="PR 1929 open RUNS [...]\n", files={"inspect.py": _FAKE_INSPECT})
        assert tid in kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready", task.status
        assert "before its first heartbeat; workspace file inspect.py shadows stdlib module inspect" in (
            task.last_failure_error)


def test_crash_after_heartbeat_or_without_shadow_is_unchanged(board):
    with kbc.connect() as conn:
        tid, _ = _dead_worker(conn, log=_TRACEBACK, files={"inspect.py": _FAKE_INSPECT}, heartbeat=True)
        tid2, _ = _dead_worker(conn, log="boom\n", files={"probe.py": ""}, pid=73001)
        crashed = kbd.detect_crashed_workers(conn)
        for t in (tid, tid2):
            assert t in crashed
            task = kb.get_task(conn, t)
            assert task.status == "ready", task.status
            assert "shadows stdlib" not in (task.last_failure_error or "")
