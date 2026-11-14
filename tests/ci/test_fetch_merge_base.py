"""fetch-merge-base: a depth-1 checkout plus a commits-only fetch answers like a full clone.

The CI jobs that need a merge base (prompt-surface-diff, lockfile-diff, history-check,
contributor-check, lint's ruff + ty diff, supply-chain-audit) used `fetch-depth: 0`, which on the
fork pulled every head, tag and blob and ran past the ci-fast timeouts. They now check out at depth
1 and run .github/actions/fetch-merge-base. These tests drive its script against a local origin
(file://, partial-clone filters allowed, as GitHub allows them) from a depth-1 clone.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ci.workflow_steps import replay_bash

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


def run_script(cwd: Path, *args: str, path_prefix: Path | None = None,
               extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **GIT_ENV, **(extra_env or {})}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run([replay_bash(), str(SCRIPT), *args], cwd=cwd, env=env, capture_output=True,
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


# git 2.55.0 on the Blacksmith runner, 2026-11-13 (runs 43383623029 and 43427608141): the filtered
# --unshallow fetch made index-pack "repack local links" in promisor mode, and pack-objects died.
GIT_255_BUG = """\
BUG: builtin/pack-objects.c:5004: should_include_obj should only be called on existing objects
error: pack-objects died of signal 6
fatal: could not finish pack-objects to repack local links
fatal: index-pack failed
"""

PARTIAL_CLONE_KEYS = ("remote.origin.promisor", "remote.origin.partialclonefilter",
                      "extensions.partialclone")

SHIM = """\
#!/usr/bin/env bash
printf '%s\\x1f' "$@" >> "$FMB_ARGV_LOG"
printf '\\n' >> "$FMB_ARGV_LOG"
is_fetch=0; filtered=0
for a in "$@"; do
  [[ $a == fetch ]] && is_fetch=1
  [[ $a == --filter=tree:0 ]] && filtered=1
done
if [[ ${FMB_FAIL_FILTERED:-} == 1 && $is_fetch == 1 && $filtered == 1 ]]; then
  # What the real filtered fetch registers before index-pack dies.
  "$FMB_REAL_GIT" config core.repositoryformatversion 1
  "$FMB_REAL_GIT" config remote.origin.promisor true
  "$FMB_REAL_GIT" config remote.origin.partialclonefilter tree:0
  "$FMB_REAL_GIT" config extensions.partialclone origin
  cat "$FMB_BUG_TEXT" >&2
  exit 128
fi
exec "$FMB_REAL_GIT" "$@"
"""


def git_shim(tmp_path: Path, fail_filtered: bool) -> tuple[Path, Path, dict[str, str]]:
    """A `git` on PATH that logs every argv and passes it to the real git.

    With fail_filtered, a `fetch ... --filter=tree:0 ...` instead registers the partial clone,
    prints the git 2.55 BUG text and exits 128, as on the runner.
    """
    real_git = shutil.which("git")
    assert real_git
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "git").write_text(SHIM, encoding="utf-8")
    (shim_dir / "git").chmod(0o755)
    log = tmp_path / "git-argv.log"
    bug = tmp_path / "bug.txt"
    bug.write_text(GIT_255_BUG, encoding="utf-8")
    env = {"FMB_ARGV_LOG": str(log), "FMB_REAL_GIT": real_git, "FMB_BUG_TEXT": str(bug),
           "FMB_FAIL_FILTERED": "1" if fail_filtered else ""}
    return shim_dir, log, env


def fetch_calls(log: Path) -> list[list[str]]:
    calls = [line.split("\x1f")[:-1] for line in log.read_text(encoding="utf-8").splitlines()]
    return [c for c in calls if "fetch" in c]


def config_get(work: Path, key: str) -> int:
    return subprocess.run(["git", "config", "--get", key], cwd=work,
                          env={**os.environ, **GIT_ENV}, capture_output=True,
                          check=False).returncode


def test_git_255_pack_objects_bug_falls_back_to_a_full_history_fetch(origin, tmp_path):
    """T1: the filtered fetch dies with the git 2.55 BUG; one unfiltered fetch answers instead."""
    work = checkout(origin, tmp_path, "feature")
    head = git(work, "rev-parse", "HEAD")
    shim_dir, log, env = git_shim(tmp_path, fail_filtered=True)

    result = run_script(work, "main", path_prefix=shim_dir, extra_env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == git(origin, "merge-base", "main", "feature")
    assert ("fetch-merge-base: filtered fetch failed (rc=128): fatal: index-pack failed; "
            "full-history fallback") in result.stderr.splitlines()
    filtered, fallback = fetch_calls(log)
    assert "--filter=tree:0" in filtered
    assert not any(a.startswith("--filter") for a in fallback)
    assert fallback == ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--unshallow",
                        "origin", head, "+refs/heads/main:refs/remotes/origin/main"]
    for key in PARTIAL_CLONE_KEYS:
        assert config_get(work, key) == 1, key
    assert git(work, "rev-parse", "--is-shallow-repository") == "false"
    assert git(work, "show", "refs/remotes/origin/main:m6.txt") == "m6"


def config_values(work: Path) -> dict[str, str | None]:
    out = {}
    for key in PARTIAL_CLONE_KEYS:
        got = subprocess.run(["git", "config", "--get", key], cwd=work,
                             env={**os.environ, **GIT_ENV}, capture_output=True,
                             encoding="utf-8", check=False)
        out[key] = got.stdout.strip() if got.returncode == 0 else None
    return out


def guards_checkout(origin: Path, tmp_path: Path) -> tuple[Path, str]:
    """repo-guards: depth-2 checkout, then call 1 (`main`) with the real git. Returns the PR head."""
    work = tmp_path / "work-guards"
    git(tmp_path, "clone", "-q", "--depth=2", "--no-tags", "--single-branch", "-b", "main",
        origin.as_uri(), str(work))
    first = run_script(work, "main")
    assert first.returncode == 0, first.stderr
    assert config_values(work)["remote.origin.promisor"] == "true"
    return work, git(origin, "rev-parse", "feature")


def test_second_call_fallback_keeps_the_partial_clone_readable(origin, tmp_path):
    """The incident path: call 1 made a partial clone; call 2's filtered fetch dies.

    The fallback must restore the promisor config, or the base history's trees (fetched commits
    only by call 1) can never be read and `git diff base...head` fails with no output.
    """
    work, head = guards_checkout(origin, tmp_path)
    before = config_values(work)
    shim_dir, log, env = git_shim(tmp_path, fail_filtered=True)

    result = run_script(work, "main", head, path_prefix=shim_dir, extra_env=env)

    assert result.returncode == 0, result.stderr
    merge_base = git(origin, "merge-base", "main", "feature")
    assert result.stdout.strip() == merge_base
    assert "full-history fallback" in result.stderr
    filtered, fallback = fetch_calls(log)
    assert "--filter=tree:0" in filtered
    assert not any(a.startswith("--filter") for a in fallback)
    assert config_values(work) == before
    assert git(work, "ls-tree", "--name-only", merge_base).splitlines() == [
        "m1.txt", "m2.txt", "m3.txt", "m4.txt"]
    diff = ["diff", "--no-renames", "--name-only", f"origin/main...{head}"]
    assert git(work, *diff).splitlines() == ["f1.txt", "f2.txt", "f3.txt"]


def test_fallback_fails_when_the_merge_base_tree_cannot_be_read(origin, tmp_path):
    """Fail closed: a fallback that leaves an unreadable merge base must not pass the step."""
    work, head = guards_checkout(origin, tmp_path)
    for key in PARTIAL_CLONE_KEYS:  # lazy fetch impossible: call 1's base trees are unreachable
        subprocess.run(["git", "config", "--unset-all", key], cwd=work,
                       env={**os.environ, **GIT_ENV}, capture_output=True, check=False)
    shim_dir, _, env = git_shim(tmp_path, fail_filtered=True)

    result = run_script(work, "main", head, path_prefix=shim_dir, extra_env=env)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "cannot be read" in result.stderr


def test_a_failing_fallback_still_fails(origin, tmp_path):
    work = checkout(origin, tmp_path, "feature")
    shim_dir, log, env = git_shim(tmp_path, fail_filtered=True)

    result = run_script(work, "no-such-branch", path_prefix=shim_dir, extra_env=env)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "full-history fallback" in result.stderr
    assert len(fetch_calls(log)) == 2


def test_a_healthy_filtered_fetch_has_no_fallback(origin, tmp_path):
    """T2: no injected failure: one filtered fetch with the original argv, no fallback line."""
    work = checkout(origin, tmp_path, "feature")
    head = git(work, "rev-parse", "HEAD")
    shim_dir, log, env = git_shim(tmp_path, fail_filtered=False)

    result = run_script(work, "main", path_prefix=shim_dir, extra_env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == git(origin, "merge-base", "main", "feature")
    assert fetch_calls(log) == [[
        "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--filter=tree:0",
        "--unshallow", "origin", head, "+refs/heads/main:refs/remotes/origin/main"]]
    lines = [line for line in result.stderr.splitlines() if line]
    assert len(lines) == 1
    assert lines[0].startswith("fetch-merge-base: fetched refs/remotes/origin/main and ")
    assert config_get(work, "remote.origin.promisor") == 0
