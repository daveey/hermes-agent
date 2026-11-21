"""A worktree card whose working copy was deleted after it ran is restored from
its own branch at the next spawn, not given up on as a config error.

Regression (mini, 2026-11-03, self-heal kanban-9101461fb6): bb card
t_01a1a13715b5fff8 (workspace_kind=worktree, ~/code/bb-dev/bb-1175, branch
bb-dev/bb-1175, repo ~/code/bb-dev/babybear) woke from a CI wait after its tree
had been removed. ``_resolve_worktree_workspace`` found no repo above the path
and raised WorktreeWorkspaceConfigError ("use workspace_path=<repo root>"), so
the dispatcher sticky-blocked a valid card although its branch still existed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


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
        "platform_toolsets:\n  cli: [terminal, file]\nterminal:\n  backend: local\n" + _managed_yaml(), encoding="utf-8",
    )
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def bb_dev(tmp_path):
    """``<tmp>/code/bb-dev`` (not a repo) holding repo ``babybear`` and the card's sibling tree ``bb-1175``."""
    root = tmp_path / "code" / "bb-dev"
    repo = root / "babybear"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    tree = root / "bb-1175"
    _git(repo, "worktree", "add", "-b", "bb-dev/bb-1175", str(tree), "HEAD")
    (tree / "fix.txt").write_text("work\n", encoding="utf-8")
    _git(tree, "add", "fix.txt")
    _git(tree, "commit", "-m", "card work")
    return root


def _spawn(conn):
    from hermes_cli import kanban_db_dispatch as kbd
    spawned = []
    kbd.dispatch_once(conn, spawn_fn=lambda task, ws, *a, **k: spawned.append(ws) or 1234, failure_limit=3)
    return spawned


def _requeue(conn, tid):
    assert kb.reclaim_task(conn, tid, reason="test: next wake", signal_fn=lambda *a, **k: None)
    assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize("how", ["rm -rf", "git worktree remove"])
def test_deleted_worktree_is_restored_on_its_branch_at_next_spawn(kanban_home, bb_dev, all_assignees_spawnable, how):
    tree = bb_dev / "bb-1175"
    head = _git(tree, "rev-parse", "HEAD")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="BB-1175", assignee="a", workspace_kind="worktree",
                             workspace_path=str(tree), branch_name="bb-dev/bb-1175")
        assert _spawn(conn) == [str(tree.resolve())]
        _requeue(conn, tid)
        if how == "rm -rf":
            shutil.rmtree(tree)
        else:
            _git(bb_dev / "babybear", "worktree", "remove", "--force", str(tree))
        assert not tree.exists()

        spawned = _spawn(conn)
        task = kb.get_task(conn, tid)
        assert spawned == [str(tree)], task.last_failure_error
        assert task.status == "running"
        assert _git(tree, "branch", "--show-current") == "bb-dev/bb-1175"
        assert _git(tree, "rev-parse", "HEAD") == head  # same branch, nothing overwritten
        restored = [e for e in kb.list_events(conn, tid) if e.kind == "worktree_restored"]
        assert len(restored) == 1
        p = restored[0].payload
        assert p["path"] == str(tree)
        assert p["branch"] == "bb-dev/bb-1175"
        assert Path(p["repo"]).resolve() == (bb_dev / "babybear").resolve()
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]


def test_deleted_worktree_without_its_branch_names_the_deletion_not_the_config(
    kanban_home, bb_dev, all_assignees_spawnable,
):
    tree = bb_dev / "bb-1175"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="BB-1175", assignee="a", workspace_kind="worktree",
                             workspace_path=str(tree), branch_name="bb-dev/bb-1175")
        assert _spawn(conn)
        _requeue(conn, tid)
        repo = bb_dev / "babybear"
        _git(repo, "worktree", "remove", "--force", str(tree))
        _git(repo, "branch", "-D", "bb-dev/bb-1175")

        assert _spawn(conn) == []
        err = kb.get_task(conn, tid).last_failure_error
        assert "was deleted after the card ran" in err
        assert "use workspace_path=" not in err


def test_restore_never_creates_a_branch_in_a_guessed_repo(kanban_home, bb_dev, all_assignees_spawnable):
    """A never-created sibling path with no existing branch stays a config error (create-time rule)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a", workspace_kind="worktree",
                             workspace_path=str(bb_dev / "bb-9999"))
        assert _spawn(conn) == []
        assert not (bb_dev / "bb-9999").exists()
        assert "not inside a git repo" in kb.get_task(conn, tid).last_failure_error
        assert _git(bb_dev / "babybear", "branch", "--list", f"wt/{tid}") == ""
