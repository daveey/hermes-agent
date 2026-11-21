"""Worktree workspaces that can never resolve are rejected at create time and,
for legacy rows, block on the FIRST spawn failure instead of burning retries.

Regression: a worker created a card with ``workspace_path=~/code/<repo>-<slug>``
(a sibling of the repo, outside any git repo, board without default_workdir).
``_resolve_worktree_workspace`` failed identically on every dispatch, the card
spent its retry budget (spawn_failed -> gave_up) and the creator never saw why.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw

# Match by base class + name: other kanban tests ``importlib.reload`` the workspace
# module, which rebinds the class object this file imported at collection.
_CONFIG_ERR = ValueError


def _is_config_err(exc: BaseException) -> bool:
    return type(exc).__name__ == "WorktreeWorkspaceConfigError"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profile = home / "profiles" / "a"
    profile.mkdir(parents=True)
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (profile / "config.yaml").write_text(
        "platform_toolsets:\n  cli: [terminal, file]\nterminal:\n  backend: local\n" + _managed_yaml(),
        encoding="utf-8",
    )
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def code_dir(tmp_path):
    """``<tmp>/code`` (not a repo) holding repo ``babybear``."""
    code = tmp_path / "code"
    repo = code / "babybear"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return code


def test_create_rejects_sibling_path_outside_repo_and_names_fix(kanban_home, code_dir):
    bad = str(code_dir / "babybear-s4-speaker-admission")
    with kbc.connect() as conn:
        with pytest.raises(_CONFIG_ERR) as ei:
            kb.create_task(conn, title="t", assignee="a", workspace_kind="worktree",
                           workspace_path=bad, validate_workspace=True)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert _is_config_err(ei.value)
    msg = str(ei.value)
    repo = str(code_dir / "babybear")
    assert "not inside a git repo" in msg and "Fix:" in msg
    assert f"workspace_path={repo!r}" in msg  # sibling repo suggested
    assert f"git -C {repo} worktree add {bad} -b <branch>" in msg


@pytest.mark.parametrize("which", ["repo_root", "inside_repo", "linked_worktree"])
def test_create_accepts_resolvable_worktree_paths(kanban_home, code_dir, which):
    repo = code_dir / "babybear"
    if which == "repo_root":
        path = repo
    elif which == "inside_repo":
        path = repo / ".worktrees" / "new-one"  # parent need not exist yet
    else:
        path = code_dir / "babybear-feature"
        _git(repo, "worktree", "add", str(path), "-b", "feature", "HEAD")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", workspace_kind="worktree",
                             workspace_path=str(path), validate_workspace=True)
        assert kb.get_task(conn, tid).workspace_kind == "worktree"


def test_create_rejects_relative_and_missing_board_default(kanban_home):
    with kbc.connect() as conn:
        with pytest.raises(_CONFIG_ERR, match="not absolute") as rel:
            kb.create_task(conn, title="t", assignee="a", workspace_kind="worktree",
                           workspace_path="rel/path", validate_workspace=True)
        with pytest.raises(_CONFIG_ERR, match="default_workdir") as nodef:
            kb.create_task(conn, title="t", assignee="a", workspace_kind="worktree",
                           validate_workspace=True)
    assert _is_config_err(rel.value) and _is_config_err(nodef.value)


def test_create_without_validation_keeps_legacy_behaviour(kanban_home, code_dir):
    # Internal callers (decompose, tests, imports) are unchanged.
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", workspace_kind="worktree",
                             workspace_path=str(code_dir / "babybear-x"))
        assert kb.get_task(conn, tid) is not None


def test_kanban_create_tool_returns_error_to_creator(kanban_home, code_dir, monkeypatch):
    import json

    from tools import kanban_tools

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kanban_tools._handle_create({
        "title": "t", "assignee": "a", "workspace_kind": "worktree",
        "workspace_path": str(code_dir / "babybear-s4-speaker-admission"),
    }))
    assert "error" in out, out
    assert "worktree add" in out["error"]
    with kbc.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_cli_create_rejects_bad_worktree(kanban_home, code_dir, capsys):
    from hermes_cli import kanban as kcli

    ns = argparse.Namespace(
        title="t", body=None, body_file=None, assignee="a", created_by="test",
        workspace=f"worktree:{code_dir / 'babybear-s4'}", branch=None, max_runtime=None,
        max_retries=None, tenant=None, priority=0, parent=None, triage=False,
        idempotency_key=None, skills=None, json=False,
    )
    rc = kcli._cmd_create(ns)
    assert rc == 2
    assert "worktree add" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_dispatcher_blocks_config_error_on_first_failure(kanban_home, code_dir, all_assignees_spawnable):
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", workspace_kind="worktree",
                             workspace_path=str(code_dir / "babybear-s4"))
        spawned = []
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1234, failure_limit=3)
        task = kb.get_task(conn, tid)
        assert spawned == []
        assert (task.status, task.block_kind) == ("triage", "transient")
        assert task.consecutive_failures == 1
        assert "not inside a git repo" in task.last_failure_error
        assert "Fix: use workspace_path=" in task.last_failure_error
        gave_up = [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]
        assert gave_up and gave_up[-1].payload.get("sticky") is True
        assert gave_up[-1].payload.get("workspace_config_error") is True
        # Sticky: recompute_ready must not auto-resume it.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "triage"


def test_dispatcher_transient_workspace_error_still_retries(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    from hermes_cli import kanban_db_dispatch as kbd

    def boom(task, board=None):
        raise OSError("disk hiccup")

    monkeypatch.setattr(kbd._kbw, "resolve_workspace", boom)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1234, failure_limit=3)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
