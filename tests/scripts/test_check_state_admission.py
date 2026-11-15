"""Shared-hook candidate classification, without spoofing the machine's OS."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATE_FILES = ("scripts/check", "hermes-sync", "shared-config.json",
               "scripts/state-push-guard", "tests/state_guard_rules.py")


def run(repo, env, *argv):
    return subprocess.run(argv, cwd=repo, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=30, check=False)


@pytest.fixture
def candidate(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(tmp_path / "home"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    repo = tmp_path / "repo"
    repo.mkdir()
    assert run(repo, env, "git", "init", "-q").returncode == 0
    for rel in STATE_FILES:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    return repo, env


def admission(candidate, host="Darwin"):
    repo, env = candidate
    assert run(repo, env, "git", "add", ".").returncode == 0
    tree = run(repo, env, "git", "write-tree").stdout.strip()
    hook = runpy.run_path(str(ROOT / "scripts/check"))["_HOOK_HEAD"]
    # Call the actual host decision with explicit data. No uname shim, platform
    # patch or production bypass: native execution remains a separate proof.
    return run(repo, env, "bash", "-c", hook + '\ncandidate_admitted "$1" "$2"',
               "fixture", tree, host)


def test_state_candidate_is_admitted(candidate):
    result = admission(candidate)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", STATE_FILES)
def test_incomplete_or_unknown_candidate_refused(candidate, missing):
    (candidate[0] / missing).unlink()
    assert admission(candidate).returncode == 2


@pytest.mark.parametrize("marker", ["pyproject.toml", "run_agent.py", "hermes_cli", "agent"])
def test_core_and_mixed_candidates_refused(candidate, marker):
    (candidate[0] / marker).write_text("historical core", encoding="utf-8")
    assert admission(candidate).returncode == 2


@pytest.mark.parametrize("rel", STATE_FILES)
def test_symlink_state_markers_refused(candidate, rel):
    path = candidate[0] / rel
    path.unlink()
    path.symlink_to("elsewhere")
    assert admission(candidate).returncode == 2


def test_worktree_state_markers_cannot_admit_unknown_index(candidate):
    repo, env = candidate
    assert run(repo, env, "git", "add", "scripts/check").returncode == 0
    tree = run(repo, env, "git", "write-tree").stdout.strip()
    hook = runpy.run_path(str(ROOT / "scripts/check"))["_HOOK_HEAD"]
    result = run(repo, env, "bash", "-c", hook + '\ncandidate_admitted "$1" Darwin', "fixture", tree)
    assert result.returncode == 2


def test_linux_unknown_candidate_is_admitted(candidate):
    (candidate[0] / "hermes-sync").unlink()
    assert admission(candidate, "Linux").returncode == 0


@pytest.mark.parametrize("rel", STATE_FILES)
def test_directory_state_markers_refused(candidate, rel):
    path = candidate[0] / rel
    path.unlink()
    path.mkdir()
    (path / "child").write_text("fixture", encoding="utf-8")
    assert admission(candidate).returncode == 2


def test_executable_state_files_admitted(candidate):
    for rel in STATE_FILES:
        (candidate[0] / rel).chmod(0o755)
    assert admission(candidate).returncode == 0


def test_admitted_candidate_runs_checker_and_preserves_failure(candidate):
    repo, env = candidate
    (repo / "scripts/check").write_text('print("candidate checker ran")\nraise SystemExit(7)\n',
                                        encoding="utf-8")
    assert run(repo, env, "git", "add", ".").returncode == 0
    tree = run(repo, env, "git", "write-tree").stdout.strip()
    hook = runpy.run_path(str(ROOT / "scripts/check"))["_HOOK_HEAD"]
    # Explicit Darwin classification followed by the real snapshot executor on
    # this pool host. This is not native macOS coverage.
    result = run(repo, env, "bash", "-c",
                 hook + '\ncandidate_admitted "$1" Darwin && judge "$1" --staged', "fixture", tree)
    assert result.returncode == 7, result.stderr
    assert "candidate checker ran" in result.stdout
