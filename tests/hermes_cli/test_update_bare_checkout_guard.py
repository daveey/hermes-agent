"""hermes update must recover a work tree whose config was flipped to core.bare=true.

2026-10-28 06:16Z the live checkout at ~/.hermes/hermes-agent reported
``rev-parse --is-bare-repository`` = true. The 06:26 hub update then died in
``_stash_local_changes_if_needed``:

    git status --porcelain -z
    fatal: this operation must be run in a work tree

On git 2.55 the flag is written by ``git init --bare`` with no path when the
cwd is the repo's .git (or a worktree gitdir under it), or when GIT_DIR names
that directory. A genuine bare repo has no ``.git`` inside the directory the
updater is standing in and must be left alone.
"""
from pathlib import Path
import subprocess

import pytest

from hermes_cli.update_cmd_stash import (
    _stash_local_changes_if_needed,
    recover_flipped_bare_checkout,
)


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True,
    )


def _worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "install"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "c")
    return repo


def _flip_bare(repo: Path) -> None:
    """The verified writer: ``git init --bare`` with no path and cwd = .git."""
    flipped = _git(repo / ".git", "init", "-q", "--bare", check=False)
    assert flipped.returncode == 0, flipped.stderr
    assert _git(repo, "rev-parse", "--is-bare-repository").stdout.strip() == "true"


def test_stash_recovers_a_flipped_work_tree_and_warns(tmp_path, capsys):
    repo = _worktree(tmp_path)
    _flip_bare(repo)
    (repo / "f").write_text("dirty\n", encoding="utf-8")
    status = _git(repo, "status", "--porcelain", "-z", check=False)
    assert status.returncode != 0
    assert "work tree" in status.stderr

    ref = _stash_local_changes_if_needed(["git"], repo)

    assert _git(repo, "rev-parse", "--is-bare-repository").stdout.strip() == "false"
    assert ref
    assert "core.bare=true" in capsys.readouterr().out
    assert _git(repo, "status", "--porcelain", "-z").returncode == 0


def test_recover_is_a_noop_when_already_non_bare(tmp_path, capsys):
    repo = _worktree(tmp_path)
    assert recover_flipped_bare_checkout(["git"], repo) is False
    assert "core.bare=true" not in capsys.readouterr().out
    assert _git(repo, "config", "--get", "core.bare").stdout.strip() == "false"


def test_recover_leaves_a_genuine_bare_repo_alone(tmp_path, capsys):
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    assert recover_flipped_bare_checkout(["git"], bare) is False
    assert _git(bare, "rev-parse", "--is-bare-repository").stdout.strip() == "true"
    assert "core.bare=true" not in capsys.readouterr().out


def test_recover_resets_via_worktree_gitdir_flip(tmp_path, capsys):
    """``git init --bare`` with cwd = ``.git/worktrees/<name>`` flips the shared config."""
    repo = _worktree(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
    gitdir = Path(_git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    flipped = _git(gitdir, "init", "-q", "--bare", check=False)
    assert flipped.returncode == 0, flipped.stderr
    assert _git(repo, "rev-parse", "--is-bare-repository").stdout.strip() == "true"

    assert recover_flipped_bare_checkout(["git"], repo) is True
    assert _git(repo, "rev-parse", "--is-bare-repository").stdout.strip() == "false"
    assert "WARNING" in capsys.readouterr().out
