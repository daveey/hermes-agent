"""Module-form Hermes children import the install's tree, never one in their cwd.

A hermes-agent worktree card runs its worker with ``cwd`` = the worktree. With
``python -m hermes_cli.main`` Python put that cwd first on ``sys.path``, ahead of the
install-root ``PYTHONPATH`` pin, so the worker imported the WORKTREE's ``hermes_cli``,
took it as its project root and refused to start ("no dependency environment is
committed for this install"). Every such card died at spawn.

Each test captures the argv + env one entry path really spawns with, then runs that
argv's interpreter prefix (everything before ``-m``) with cwd = a decoy checkout whose
``hermes_cli`` / ``cron`` packages refuse to import, and asserts the child imported the
install's packages instead. ``-c`` and ``-m`` share the cwd-first ``sys.path`` rule, so the
probe sees exactly what the module launch would.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = "import hermes_cli, cron; print(hermes_cli.__file__); print(cron.__file__)"
# The spawn paths under test get ``subprocess.Popen`` monkeypatched; the probe needs the real one.
_REAL_POPEN = subprocess.Popen


def _run(argv: list[str], *, cwd: Path, env: dict) -> tuple[int, str, str]:
    with _REAL_POPEN(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True) as proc:
        out, err = proc.communicate(timeout=60)
    return proc.returncode, out, err


@pytest.fixture
def decoy_checkout(tmp_path):
    """A directory that looks like another Hermes checkout to ``-m``'s cwd entry."""
    decoy = tmp_path / "other-checkout"
    for package in ("hermes_cli", "cron"):
        (decoy / package).mkdir(parents=True)
        (decoy / package / "__init__.py").write_text(
            f"raise SystemExit('DECOY {package} imported from the cwd')\n", encoding="utf-8"
        )
    return decoy


def _imports_from(argv: list[str], env: dict, cwd: Path) -> list[Path]:
    """Run ``argv``'s interpreter + flags with the probe; return the imported package files."""
    prefix = argv[: argv.index("-m")]
    child_env = {k: v for k, v in env.items() if k != "PYTHONSAFEPATH"}
    returncode, out, err = _run([*prefix, "-c", PROBE], cwd=cwd, env=child_env)
    assert returncode == 0, (out, err)
    return [Path(line).resolve() for line in out.split()]


def _assert_install_tree(paths: list[Path]) -> None:
    assert paths, "probe printed nothing"
    for path in paths:
        assert path.is_relative_to(REPO_ROOT), f"{path} is not from the install tree {REPO_ROOT}"


# --- kanban worker spawn ----------------------------------------------------------------


def _kanban_task(kb):
    return kb.Task(
        id="t_argv", title="argv", body=None, assignee="w", status="running", priority=0,
        created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="worktree", workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=1,
    )


def _capture_kanban_spawn(monkeypatch, tmp_path, workspace: Path) -> dict:
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_BIN", raising=False)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    # The systemd scope wrapper is a prefix around the same argv; keep the test host-neutral.
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda task, cmd, board=None: cmd)
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured.update(cmd=list(cmd), env=dict(kwargs.get("env") or {}), cwd=kwargs.get("cwd"))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kbd._default_spawn(_kanban_task(kb), str(workspace))
    return captured


def test_kanban_worker_in_a_checkout_workspace_imports_the_install(
    monkeypatch, tmp_path, decoy_checkout
):
    captured = _capture_kanban_spawn(monkeypatch, tmp_path, decoy_checkout)

    assert captured["cwd"] == str(decoy_checkout)
    assert "hermes_cli.main" in captured["cmd"]
    _assert_install_tree(_imports_from(captured["cmd"], captured["env"], decoy_checkout))
    # And the real entry point starts from there (it refused with the PM error before).
    module_at = captured["cmd"].index("hermes_cli.main")
    child_env = {k: v for k, v in captured["env"].items() if k != "PYTHONSAFEPATH"}
    returncode, out, err = _run([*captured["cmd"][: module_at + 1], "--version"],
                                cwd=decoy_checkout, env=child_env)
    assert returncode == 0 and "DECOY" not in err, (out, err)


def test_placement_forwarder_and_its_worker_import_the_install(
    monkeypatch, tmp_path, decoy_checkout
):
    """Deferred placement swaps in ``-m hermes_cli.kanban_placement_forward -- <worker argv>``;
    both the forwarder and the worker argv it execs run from the task workspace."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement_forward as fwd

    monkeypatch.setattr(fwd, "state", lambda conn, task_id: {"deferred": True})
    real_db_path = kb.kanban_db_path

    def db_path(board=None):
        path = Path(real_db_path(board=board))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    monkeypatch.setattr(kb, "kanban_db_path", db_path)
    captured = _capture_kanban_spawn(monkeypatch, tmp_path, decoy_checkout)

    cmd = captured["cmd"]
    assert fwd.MODULE in cmd, cmd
    _assert_install_tree(_imports_from(cmd, captured["env"], decoy_checkout))
    worker = cmd[cmd.index("--") + 1:]
    assert "hermes_cli.main" in worker
    _assert_install_tree(_imports_from(worker, captured["env"], decoy_checkout))


# --- cron external worker ---------------------------------------------------------------


def test_cron_external_worker_argv_imports_the_install(monkeypatch, tmp_path, decoy_checkout):
    """The worker is spawned from the install root today; its argv must not depend on that."""
    import json
    from unittest.mock import Mock

    from cron import scheduler
    from tools.process_registry import GatewayChildDispatch

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "tools.process_registry.restart_safe_gateway_child_argv",
        lambda command, **_: GatewayChildDispatch("degraded", command),
    )
    spawned: list = []

    class FakeProcess:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    def popen(command, **kwargs):
        if "--ack-file" not in command:  # an unrelated helper spawn
            return _REAL_POPEN(command, **kwargs)
        spawned.append((list(command), kwargs))
        ack = Path(command[command.index("--ack-file") + 1])
        ack.write_text(json.dumps({"pid": 4321, "execution_id": "exec-1"}), encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(scheduler, "mark_execution_handoff_pending",
                        Mock(return_value={"id": "exec-1", "handoff_pending": 1}))
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)
    statuses = iter([{"id": "exec-1", "status": "running"}, {"id": "exec-1", "status": "completed"}])
    monkeypatch.setattr(scheduler, "get_execution", Mock(side_effect=lambda _id: next(statuses)))

    assert scheduler._launch_external_cron_worker(
        {"id": "job-1", "execution_id": "exec-1", "prompt": "work"}) is True
    command, kwargs = spawned[0]
    assert "cron.scheduler" in command
    _assert_install_tree(_imports_from(command, kwargs["env"], decoy_checkout))


# --- cron bot-chat delivery -------------------------------------------------------------


def test_cron_bot_chat_turn_argv_imports_the_install(monkeypatch, tmp_path, decoy_checkout):
    import cron.scheduler_delivery as delivery

    root = tmp_path / "hermes"
    (root / "profiles" / "beta").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    captured: dict = {}

    def fake_turn(argv, env, report_path, timeout):
        captured.update(argv=list(argv), env=dict(env))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(delivery, "_run_bot_chat_turn", fake_turn)
    assert delivery._deliver_to_bot_chat({"id": "j", "name": "nightly"}, "brief", "beta") is None

    assert "hermes_cli.main" in captured["argv"]
    _assert_install_tree(_imports_from(captured["argv"], captured["env"], decoy_checkout))


def test_safe_path_flag_is_not_inherited_by_grandchildren(tmp_path):
    """``-P`` is an interpreter flag, not ``PYTHONSAFEPATH``: the worker's own terminal
    children keep the normal cwd-first ``python -m`` semantics."""
    from cron.scheduler_worker_env import hermes_module_argv

    import sys

    argv = hermes_module_argv(sys.executable)
    prefix = argv[: argv.index("-m")]
    grandchild = "import sys; print(sys.flags.safe_path)"
    script = (
        "import subprocess, sys; print(sys.flags.safe_path); "
        f"print(subprocess.run([sys.executable, '-c', {grandchild!r}], "
        "capture_output=True, text=True).stdout.strip())"
    )
    result = subprocess.run(
        [*prefix, "-c", script],
        cwd=str(tmp_path), env={k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.stdout.split() == ["True", "False"], result
