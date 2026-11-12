"""A delegated child without git worktree isolation shares its parent's cwd; it
gets its own private scratch folder, named on its goal turn together with the
warning that the inherited cwd is SHARED (self-heal agent-c97f5ffde4). The system
prompt is left alone so siblings keep one shared prompt cache."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import subagent_worktree
from tools.delegate_tool_child_run import _ChildRun
from tools.delegate_tool_progress import _build_private_scratch_block
from tools.terminal_tool import clear_session_cwd, get_session_cwd, record_session_cwd

_BASE_PROMPT = "You are a focused subagent working on a specific delegated task."


def _seed(parent_task_id: str, subagent_id: str) -> tuple[_ChildRun, SimpleNamespace]:
    child = SimpleNamespace(ephemeral_system_prompt=_BASE_PROMPT, _delegate_profile_home=None)
    parent = SimpleNamespace(_current_task_id=parent_task_id)
    run = _ChildRun(child, parent, 0, "do the thing", subagent_id, None)
    run.seed_workspace()
    return run, child


def _scratch_tmp(tmp_path: Path, monkeypatch) -> Path:
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    return tmpdir


def test_non_git_children_get_distinct_private_scratch_named_in_prompt(tmp_path, monkeypatch):
    tmpdir = _scratch_tmp(tmp_path, monkeypatch)
    shared = tmp_path / "shared-scratch"
    shared.mkdir()
    record_session_cwd("parent-ps-1", str(shared))
    runs = []
    try:
        for sid in ("sa-0-aaaa1111", "sa-1-bbbb2222"):
            runs.append(_seed("parent-ps-1", sid))
        dirs = []
        for (run, child), sid in zip(runs, ("sa-0-aaaa1111", "sa-1-bbbb2222")):
            private = run.private_scratch
            assert private and os.path.isdir(private)
            assert Path(private).parent == tmpdir
            assert Path(private).name.startswith(f"subagent-{sid}-")
            assert child.ephemeral_system_prompt == _BASE_PROMPT
            prompt = run.goal
            assert prompt.startswith("do the thing\n\nPRIVATE SCRATCH: ")
            assert "PRIVATE SCRATCH" in prompt and private in prompt
            assert "SHARED" in prompt and str(shared) in prompt
            # The session cwd stays the parent's: brief paths are relative to it.
            assert get_session_cwd(run.child_task_id) == str(shared)
            assert run.worktree_info is None
            dirs.append(private)
        assert dirs[0] != dirs[1]
        # Each prompt names only its own private folder.
        assert dirs[1] not in runs[0][0].goal
        assert dirs[0] not in runs[1][0].goal
    finally:
        clear_session_cwd("parent-ps-1")
        for run, _ in runs:
            clear_session_cwd(run.child_task_id)


def test_unrecorded_parent_cwd_falls_back_to_workspace_hint(tmp_path, monkeypatch):
    _scratch_tmp(tmp_path, monkeypatch)
    hinted = tmp_path / "hinted"
    hinted.mkdir()
    child = SimpleNamespace(ephemeral_system_prompt=None, _delegate_profile_home=None)
    parent = SimpleNamespace(_current_task_id="parent-ps-4-unrecorded", terminal_cwd=str(hinted))
    run = _ChildRun(child, parent, 0, "goal", "sa-0-eeee5555", None)
    try:
        run.seed_workspace()
        assert child.ephemeral_system_prompt is None
        prompt = run.goal
        assert prompt.startswith("goal\n\nPRIVATE SCRATCH: ")
        assert f"Your working directory {hinted} is SHARED" in prompt
    finally:
        clear_session_cwd(run.child_task_id)


def test_block_without_known_cwd_still_says_shared():
    block = _build_private_scratch_block("/tmp/x", None)
    assert block.startswith("PRIVATE SCRATCH: /tmp/x (yours alone). Your working directory is SHARED")


def test_git_isolated_child_gets_no_private_scratch_block(tmp_path, monkeypatch):
    tmpdir = _scratch_tmp(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run([*git, "add", "f.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    record_session_cwd("parent-ps-2", str(repo))
    run = None
    try:
        with mock.patch("tools.delegate_tool._get_worktree_isolation", return_value=True), \
                mock.patch.object(subagent_worktree, "local_backend_active", return_value=True):
            run, child = _seed("parent-ps-2", "sa-0-cccc3333")
        assert run.worktree_info is not None
        assert run.private_scratch is None
        assert child.ephemeral_system_prompt == _BASE_PROMPT
        assert "PRIVATE SCRATCH" not in run.goal
        assert list(tmpdir.iterdir()) == []
    finally:
        if run is not None and run.worktree_info is not None:
            subagent_worktree.finalize_subagent_worktree(run.worktree_info)
        clear_session_cwd("parent-ps-2")
        if run is not None:
            clear_session_cwd(run.child_task_id)


def test_non_local_backend_gets_no_host_private_scratch(tmp_path, monkeypatch):
    """A host TMPDIR folder is invisible to a docker/ssh child's tools: naming it would mislead."""
    tmpdir = _scratch_tmp(tmp_path, monkeypatch)
    shared = tmp_path / "shared"
    shared.mkdir()
    record_session_cwd("parent-ps-3", str(shared))
    run = None
    try:
        with mock.patch.object(subagent_worktree, "local_backend_active", return_value=False):
            run, child = _seed("parent-ps-3", "sa-0-dddd4444")
        assert run.private_scratch is None
        assert child.ephemeral_system_prompt == _BASE_PROMPT
        assert "PRIVATE SCRATCH" not in run.goal
        assert list(tmpdir.iterdir()) == []
    finally:
        clear_session_cwd("parent-ps-3")
        if run is not None:
            clear_session_cwd(run.child_task_id)
