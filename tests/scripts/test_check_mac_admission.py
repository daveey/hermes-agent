"""Pool-only Darwin decision fixtures; never run a portable suite on a Mac."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.platforms("linux", "macos")
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("args", [[], ["--only", "health"], ["--commit", "HEAD"], ["--staged"]])
def test_check_refuses_darwin_before_guards(monkeypatch, capsys, args):
    ns = runpy.run_path(str(ROOT / "scripts/check"))
    run = Mock(return_value={})
    upstream = Mock(return_value=False)
    monkeypatch.setitem(ns["main"].__globals__, "_run_all", run)
    monkeypatch.setitem(ns["main"].__globals__, "_upstream_sync_refused", upstream)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ns["main"](args) == 2
    assert "pool worker" in capsys.readouterr().err
    run.assert_not_called()
    upstream.assert_not_called()


def test_check_management_and_linux_execution_remain_available(monkeypatch):
    ns = runpy.run_path(str(ROOT / "scripts/check"))
    install, uninstall = Mock(return_value=0), Mock(return_value=0)
    run = Mock(return_value={})
    monkeypatch.setitem(ns["main"].__globals__, "install_hook", install)
    monkeypatch.setitem(ns["main"].__globals__, "uninstall_hooks", uninstall)
    monkeypatch.setitem(ns["main"].__globals__, "_run_all", run)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ns["main"](["--install-hook", "pre-push"]) == 0
    assert ns["main"](["--uninstall-hook"]) == 0
    install.assert_called_once_with("pre-push")
    uninstall.assert_called_once_with()
    run.assert_not_called()
    monkeypatch.setattr(sys, "platform", "linux")
    assert ns["main"]([]) == 0
    run.assert_called_once()


def _run(argv, cwd, env):
    return subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)


@pytest.mark.parametrize("caller", ["terminal", "execute_code"])
@pytest.mark.parametrize("host,candidate,ref", [
    ("Darwin", True, "refs/core-wt-test/fixture"),
    ("Darwin", True, "refs/heads/topic"),
    ("Darwin", True, "refs/tags/v1"),
    ("Darwin", False, "refs/heads/state"),
    ("Linux", True, "refs/core-wt-test/fixture"),
])
def test_git_snapshot_push_refuses_before_historical_checker(tmp_path, caller, host, candidate, ref):
    """Real Git -> generated hook -> old candidate, via shell and Python callers."""
    ns = runpy.run_path(str(ROOT / "scripts/check"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(tmp_path / "home"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    shim = tmp_path / "bin"
    shim.mkdir()
    uname = shim / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '{host}\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    repo = tmp_path / "repo"
    repo.mkdir()
    remote = tmp_path / "remote.git"
    for argv in (["git", "init", "-q", "-b", "main"],
                 ["git", "config", "user.name", "Fixture"],
                 ["git", "config", "user.email", "fixture@example.com"]):
        result = _run(argv, repo, env)
        assert result.returncode == 0, result.stderr
    scripts = repo / "scripts"
    scripts.mkdir()
    sentinel = tmp_path / "checker-started"
    (scripts / ("check" if candidate else "other")).write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8")
    for argv in (["git", "add", "."], ["git", "commit", "-qm", "historical checker"],
                 ["git", "init", "-q", "--bare", str(remote)]):
        result = _run(argv, repo, env)
        assert result.returncode == 0, result.stderr
    hook = repo / ".git/hooks/pre-push"
    hook.write_text(ns["_HOOKS"]["pre-push"], encoding="utf-8")
    hook.chmod(0o755)
    push = ["git", "push", str(remote), f"HEAD:{ref}"]
    argv = (["bash", "-c", shlex.join(push)] if caller == "terminal" else
            [sys.executable, "-c", "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:]))", *push])
    result = _run(argv, repo, env)
    refused = host == "Darwin" and candidate
    assert (result.returncode != 0) == refused, result.stdout + result.stderr
    assert ("pool worker" in result.stderr) == refused
    assert sentinel.exists() == (candidate and not refused)
    refs = _run(["git", "--git-dir", str(remote), "show-ref"], repo, env)
    assert refs.returncode == (1 if refused else 0), refs.stdout
