"""fetch-merge-base: a depth-1 checkout plus a commits-only fetch answers like a full clone.

The CI jobs that need a merge base (prompt-surface-diff, lockfile-diff, history-check,
contributor-check, lint's ruff + ty diff, supply-chain-audit) used `fetch-depth: 0`, which on the
fork pulled every head, tag and blob and ran past the ci-fast timeouts. They now check out at depth
1 and run .github/actions/fetch-merge-base. These tests drive its script against a local origin
(file://, partial-clone filters allowed, as GitHub allows them) from a depth-1 clone.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/actions/fetch-merge-base/fetch-merge-base.sh"

pytestmark = pytest.mark.platforms("posix")

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "a",
    "GIT_COMMITTER_NAME": "c",
    "GIT_COMMITTER_EMAIL": "c@example.com",
}


def git(cwd: Path, *args: str, email: str = "a@example.com") -> str:
    env = {**os.environ, **GIT_ENV, "GIT_AUTHOR_EMAIL": email}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          encoding="utf-8").stdout.strip()


def commit(repo: Path, name: str, email: str = "a@example.com") -> str:
    (repo / f"{name}.txt").write_text(name, encoding="utf-8")
    git(repo, "add", f"{name}.txt")
    git(repo, "commit", "-q", "-m", name, email=email)
    return git(repo, "rev-parse", "HEAD")


def run_script(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd, env=env, capture_output=True,
                          encoding="utf-8", timeout=120, check=False)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """main: m1..m6. feature: f1 off m2, then merges m4 (a sync), then f2 and f3 by a new author."""
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "uploadpack.allowFilter", "true")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    for i in (1, 2):
        commit(repo, f"m{i}")
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "f1")
    git(repo, "checkout", "-q", "main")
    for i in (3, 4):
        commit(repo, f"m{i}")
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-edit", "main")
    commit(repo, "f2", email="new@example.com")
    commit(repo, "f3", email="new@example.com")
    git(repo, "checkout", "-q", "main")
    for i in (5, 6):
        commit(repo, f"m{i}")
    git(repo, "checkout", "-q", "--orphan", "unrelated")
    git(repo, "rm", "-rq", "--cached", ".")
    commit(repo, "u1")
    git(repo, "checkout", "-q", "-f", "main")
    return repo


def checkout(origin: Path, tmp_path: Path, branch: str) -> Path:
    """What actions/checkout does by default: depth 1, no tags, one ref."""
    work = tmp_path / f"work-{branch}"
    git(tmp_path, "clone", "-q", "--depth=1", "--no-tags", "--single-branch", "-b", branch,
        origin.as_uri(), str(work))
    assert git(work, "rev-parse", "--is-shallow-repository") == "true"
    return work


def test_merge_base_and_ranges_match_a_full_clone(origin, tmp_path):
    work = checkout(origin, tmp_path, "feature")

    result = run_script(work, "main")

    assert result.returncode == 0, result.stderr
    expected = git(origin, "merge-base", "main", "feature")
    assert result.stdout.strip() == expected
    assert git(work, "rev-parse", "refs/remotes/origin/main") == git(origin, "rev-parse", "main")
    assert git(work, "rev-parse", "--is-shallow-repository") == "false"
    # contributor-check's range walk sees exactly the PR's commits: the m3/m4 sync merged into the
    # branch is reachable from the merge base, so it is not listed as the PR's work.
    log = ["log", f"{expected}..HEAD", "--format=%s %ae", "--no-merges"]
    assert git(work, *log) == git(origin, *log[:1], f"{expected}..feature", *log[2:])
    assert git(work, *log).splitlines() == ["f3 new@example.com", "f2 new@example.com",
                                            "f1 a@example.com"]


def test_base_trees_and_blobs_load_on_first_use(origin, tmp_path):
    """Commits only are fetched; archive/diff of the merge base still work (lazy fetch)."""
    work = checkout(origin, tmp_path, "feature")
    base = run_script(work, "main").stdout.strip()

    assert git(work, "show", f"{base}:m4.txt") == "m4"
    # m6 exists only on main past the merge base: its tree and blob were never fetched.
    assert git(work, "show", "refs/remotes/origin/main:m6.txt") == "m6"
    assert git(work, "diff", "--name-only", f"{base}...HEAD").splitlines() == [
        "f1.txt", "f2.txt", "f3.txt"]


def test_sha_inputs_fetch_a_head_the_checkout_lacks(origin, tmp_path):
    """supply-chain-audit passes the PR's base and head shas; the checkout has neither."""
    work = checkout(origin, tmp_path, "main")
    base_sha = git(origin, "rev-parse", "main~1")
    head_sha = git(origin, "rev-parse", "feature")

    result = run_script(work, base_sha, head_sha)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == git(origin, "merge-base", base_sha, head_sha)


def test_unrelated_histories_print_nothing_and_succeed(origin, tmp_path):
    work = checkout(origin, tmp_path, "unrelated")

    result = run_script(work, "main")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_fetch_failure_fails_rather_than_reading_as_unrelated(origin, tmp_path):
    """history-check treats empty output as unrelated histories: an error must not look like it."""
    work = checkout(origin, tmp_path, "feature")

    result = run_script(work, "no-such-branch")

    assert result.returncode != 0
    assert result.stdout == ""
