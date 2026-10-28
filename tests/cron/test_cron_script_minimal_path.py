"""A cron script's PATH must not depend on which process won the tick lock (hub cron-730227131a).

During a gateway restart, a Desktop-over-SSH ``hermes serve`` backend takes the cron tick lock. Its
PATH comes from a non-login SSH shell and lacks ``~/.local/bin``, so no-agent scripts calling
``token-broker`` failed with ``FileNotFoundError`` only on those runs. The terminal tool already
appends the sane PATH entries (#111778); cron scripts must get the same normalisation, and every
run document must name the process that ran it so a runner-dependent failure is visible.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

pytestmark = pytest.mark.platforms("posix")


def _tool(directory: Path, name: str, output: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text(f"#!/bin/sh\necho {output}\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tool


@pytest.fixture
def minimal_path_home(tmp_path, monkeypatch):
    """HERMES_HOME + a HOME whose ~/.local/bin holds ``fake-tool``; process PATH = SSH-minimal."""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "scripts").mkdir(parents=True)
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    home = tmp_path / "home"
    _tool(home / ".local" / "bin", "fake-tool", "from-local-bin")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return hermes_home


_CALL_TOOL = (
    "import os, subprocess\n"
    "print(subprocess.run(['fake-tool'], capture_output=True, text=True, check=True).stdout.strip())\n"
    "print(os.environ['PATH'])\n"
)


def test_script_finds_user_local_bin_tool_under_minimal_path(minimal_path_home):
    from cron.scheduler_script import _run_job_script

    script = minimal_path_home / "scripts" / "uses_tool.py"
    script.write_text(_CALL_TOOL)

    success, output = _run_job_script(str(script))
    assert success is True, output
    assert output.splitlines()[0] == "from-local-bin"


def test_user_path_entry_keeps_precedence(minimal_path_home, tmp_path, monkeypatch):
    from cron.scheduler_script import _run_job_script

    user_bin = tmp_path / "userbin"
    _tool(user_bin, "fake-tool", "from-user-path")
    monkeypatch.setenv("PATH", f"{user_bin}:/usr/bin:/bin")
    script = minimal_path_home / "scripts" / "uses_tool.py"
    script.write_text(_CALL_TOOL)

    success, output = _run_job_script(str(script))
    assert success is True, output
    first, child_path = output.splitlines()[:2]
    assert first == "from-user-path"
    entries = child_path.split(":")
    assert entries.index(str(user_bin)) < entries.index(str(Path(os.environ["HOME"]) / ".local" / "bin"))


def test_shell_script_finds_user_local_bin_tool(minimal_path_home):
    from cron.scheduler_script import _run_job_script

    script = minimal_path_home / "scripts" / "uses_tool.sh"
    script.write_text("#!/usr/bin/env bash\nfake-tool\n")

    success, output = _run_job_script(str(script))
    assert success is True, output
    assert output == "from-local-bin"


def test_external_worker_env_path_is_normalised(minimal_path_home, monkeypatch):
    """The restart-safe worker (``_launch_external_cron_worker``) gets the same PATH."""

    from cron import scheduler

    captured: dict = {}

    class _Stop(Exception):
        pass

    def fake_popen(argv, *a, **kw):
        captured["env"] = kw.get("env")
        raise _Stop()

    import tools.process_registry as registry

    monkeypatch.setattr(scheduler.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        registry, "restart_safe_gateway_child_argv",
        lambda command, **kw: registry.GatewayChildDispatch("degraded", command))
    monkeypatch.setattr(scheduler, "mark_execution_handoff_pending", lambda eid: {"id": eid})
    with pytest.raises(_Stop):
        scheduler._launch_external_cron_worker(
            {"id": "abc123", "execution_id": "e1", "name": "w", "prompt": "x"})
    env = captured.get("env")
    assert env is not None, "worker spawn not reached"
    local_bin = str(Path(os.environ["HOME"]) / ".local" / "bin")
    assert local_bin in env["PATH"].split(":")


def test_no_agent_run_doc_names_the_runner_process(minimal_path_home):
    import importlib

    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    from cron.jobs import create_job
    from cron.scheduler import run_job

    (minimal_path_home / "scripts" / "hi.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    job = create_job(prompt=None, schedule="every 5m", script="hi.sh", no_agent=True, deliver="local")
    success, doc, _final, _err = run_job(job)
    assert success is True
    assert "**Runner:** " in doc
    assert f"pid {os.getpid()}" in doc


def test_runner_label_names_serve_gateway_and_dashboard(monkeypatch):
    from cron.scheduler_script import cron_runner_label

    for argv, kind in [
        (["/x/bin/hermes", "serve", "--port", "1"], "serve"),
        (["/x/bin/hermes", "gateway", "run"], "gateway"),
        (["/x/bin/hermes", "dashboard"], "dashboard"),
        (["/x/hermes_cli/main.py", "--profile", "p", "gateway", "run"], "gateway"),
        (["-c", "serve", "--port", "1"], "serve"),
    ]:
        monkeypatch.setattr(sys, "argv", argv)
        assert cron_runner_label() == f"{kind} pid {os.getpid()}"
