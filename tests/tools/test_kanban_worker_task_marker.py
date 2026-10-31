"""[daveey] Tool children of a Kanban worker carry ``HERMES_KANBAN_WORKER_TASK``, never the worker identity.

The descendant scrub (``agent.delegation_context.scrub_kanban_env``) removes ``HERMES_KANBAN_TASK``
from every process a worker's tools spawn, so a nested ``hermes`` cannot act as the worker. That
left shell tooling (gh-shim attribution, bb-land card notes, the swift/xcodebuild compile cap) with
no way to tell it runs for a card. The informational marker restores that, one test per entry path:
terminal foreground, background process (process registry), execute_code sandbox.
"""
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.delegation_context import KANBAN_WORKER_TASK_ENV
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tools.environments.local import LocalEnvironment

ROOT = Path(__file__).resolve().parents[2]
PROBE = ("import os, json; print('ENV_RESULT=' + json.dumps({k: os.environ.get(k) for k in "
         "('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_KANBAN_RUN_ID')}))")


def _row(output: str) -> dict:
    return json.loads(next(line.split("ENV_RESULT=", 1)[1] for line in output.splitlines() if "ENV_RESULT=" in line))


def _worker_env(tmp_path, monkeypatch) -> str:
    db = tmp_path / "assigned.db"
    conn = connect(db)
    own = kb.create_task(conn, title="own")
    kb.claim_task(conn, own)
    task = kb.get_task(conn, own)
    conn.close()
    for key, value in {
        "HERMES_KANBAN_DB": str(db), "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_TASK": own, "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
        "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock, "HOME": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv(KANBAN_WORKER_TASK_ENV, raising=False)
    return own


def _non_worker_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
                "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT",
                "HERMES_DELEGATED_CHILD_OWNER", KANBAN_WORKER_TASK_ENV):
        monkeypatch.delenv(key, raising=False)


def _foreground(tmp_path) -> dict:
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    terminal = LocalEnvironment(cwd=str(tmp_path))
    try:
        result = terminal.execute(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    finally:
        terminal.cleanup()
    return _row(result.get("output", ""))


def _background(tmp_path) -> dict:
    from tools.process_registry import ProcessRegistry

    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    registry = ProcessRegistry()
    session = registry.spawn_local(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", cwd=str(tmp_path))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            poll = registry.poll(session.id)
            if poll["status"] == "exited":
                break
            time.sleep(0.05)
        else:
            pytest.fail("background probe never exited")
        return _row(registry.read_log(session.id, limit=200)["output"])
    finally:
        registry.kill_process(session.id)


def _execute_code(tmp_path) -> dict:
    from tools.code_execution_env import _build_child_env

    env = _build_child_env(rpc_endpoint="fixture", rpc_token="fixture", tmpdir=str(tmp_path),
                           child_python=sys.executable)
    proc = subprocess.run([sys.executable, "-c", PROBE], env=env, cwd=tmp_path, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=45, check=False)
    assert proc.returncode == 0, proc.stderr
    return _row(proc.stdout)


ENTRY_PATHS = {"terminal_foreground": _foreground, "background_process": _background, "execute_code": _execute_code}


@pytest.mark.parametrize("path", sorted(ENTRY_PATHS))
def test_worker_tool_child_sees_marker_not_identity(tmp_path, monkeypatch, path):
    own = _worker_env(tmp_path, monkeypatch)
    row = ENTRY_PATHS[path](tmp_path)
    assert row["HERMES_KANBAN_WORKER_TASK"] == own, row
    assert row["HERMES_KANBAN_TASK"] is None and row["HERMES_KANBAN_RUN_ID"] is None, row


@pytest.mark.parametrize("path", sorted(ENTRY_PATHS))
def test_non_worker_tool_child_sees_neither_variable(tmp_path, monkeypatch, path):
    _non_worker_env(tmp_path, monkeypatch)
    row = ENTRY_PATHS[path](tmp_path)
    assert row["HERMES_KANBAN_WORKER_TASK"] is None and row["HERMES_KANBAN_TASK"] is None, row


def test_delegate_child_of_a_chat_gets_no_marker(tmp_path, monkeypatch):
    """A delegate_task child outside any worker also crosses the scrub; there is no card to name."""
    from agent.delegation_context import delegated_child_context

    _non_worker_env(tmp_path, monkeypatch)
    with delegated_child_context("child"):
        row = _foreground(tmp_path)
    assert row["HERMES_KANBAN_WORKER_TASK"] is None and row["HERMES_KANBAN_TASK"] is None, row


def test_grandchild_of_worker_terminal_keeps_marker(tmp_path, monkeypatch):
    """A shell started by a worker terminal (nested scrub, TASK already gone) still names the card."""
    own = _worker_env(tmp_path, monkeypatch)
    script = tmp_path / "nested.py"
    script.write_text(
        "import os, sys, subprocess\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from agent.delegation_context import delegated_child_subprocess_env\n"
        f"subprocess.run([sys.executable, '-c', {PROBE!r}], env=delegated_child_subprocess_env(dict(os.environ)), check=True)\n"
    )
    terminal = LocalEnvironment(cwd=str(tmp_path))
    try:
        result = terminal.execute(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    finally:
        terminal.cleanup()
    row = _row(result.get("output", ""))
    assert row["HERMES_KANBAN_WORKER_TASK"] == own and row["HERMES_KANBAN_TASK"] is None, row


def test_nested_hermes_with_only_marker_gets_no_worker_lifecycle_tools(tmp_path, monkeypatch):
    """The marker is informational: a process carrying only it is not the worker."""
    _non_worker_env(tmp_path, monkeypatch)
    env = {**os.environ, KANBAN_WORKER_TASK_ENV: "t_fixture", "HERMES_HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir()
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from agent.delegation_context import owned_kanban_task, explicit_board_intent_is_pinned\n"
        "from tools import kanban_tools as kt\n"
        "from tools.tool_search import _in_kanban_worker\n"
        "from tools.registry import registry\n"
        "import model_tools  # registers every tool\n"
        "lifecycle = ('kanban_complete', 'kanban_block', 'kanban_heartbeat', 'kanban_request_review')\n"
        "avail = {n: bool(registry.get_entry(n) and registry.get_entry(n).check_fn and registry.get_entry(n).check_fn())"
        " for n in lifecycle}\n"
        "registered = [n for n in lifecycle if registry.get_entry(n) is not None]\n"
        "print('ENV_RESULT=' + json.dumps({'owned': owned_kanban_task(), 'in_worker': _in_kanban_worker(),"
        " 'pinned': explicit_board_intent_is_pinned(), 'default_task': kt._default_task_id(None), 'avail': avail,"
        " 'registered': registered}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=90, check=False)
    assert proc.returncode == 0, proc.stderr
    row = _row(proc.stdout)
    assert row["owned"] == "" and row["in_worker"] is False and row["pinned"] is False, row
    assert row["default_task"] is None, row
    assert len(row["registered"]) == 4, row  # the check below is not vacuous
    assert not any(row["avail"].values()), row
    # Control: the same probe WITH the worker identity does see them, so the gate is what hides them.
    proc = subprocess.run([sys.executable, "-c", code], env={**env, "HERMES_KANBAN_TASK": "t_fixture"}, cwd=ROOT,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90, check=False)
    assert proc.returncode == 0, proc.stderr
    control = _row(proc.stdout)
    assert control["in_worker"] is True and all(control["avail"].values()), control


def test_worker_protocol_prompt_names_the_variable_its_shells_have():
    """The protocol prompt must not send a worker's shell commands to ``$HERMES_KANBAN_TASK``: its terminal and
    execute_code children never have it (above), so `--card $HERMES_KANBAN_TASK` expanded to a bare `--card` and
    `wt/$HERMES_KANBAN_TASK` to `wt/` (self-heal briefs failed with "argument --card: expected one argument")."""
    from agent.prompt_builder import KANBAN_GUIDANCE

    assert f"${KANBAN_WORKER_TASK_ENV}" in KANBAN_GUIDANCE
    for shell_use in ("wt/$HERMES_KANBAN_TASK", "`$HERMES_KANBAN_TASK`;"):
        assert shell_use not in KANBAN_GUIDANCE, shell_use
