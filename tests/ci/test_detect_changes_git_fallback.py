"""Run detect-changes' real classify script with a failing GitHub API.

When the GITHUB_TOKEN REST quota ran out, the compare call failed on every PR,
detect failed open, and every PR queued the single-runner macOS state lane. The
action now falls back to the checked-out merge commit's git diff, which needs no
API quota. These tests run the action's own bash against a real git merge.
"""
import os
import subprocess
from pathlib import Path

import hermes_yaml
import pytest

ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER = ROOT / "scripts/ci/classify_changes.py"


def _script():
    action = hermes_yaml.safe_load((ROOT / ".github/actions/detect-changes/action.yml").read_text())
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "classify")
    return step["run"].replace("scripts/ci/classify_changes.py", str(CLASSIFIER))


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def pr(tmp_path):
    """An origin with base, PR head and the PR merge commit, and a depth-1 checkout of the merge."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "hermes_state.py").write_text("x = 1\n")
    (origin / "old_name.py").write_text("y = 1\n")
    (origin / "README.md").write_text("base\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "base")
    base = _git(origin, "rev-parse", "HEAD")

    def make(files, renames=()):
        _git(origin, "checkout", "-q", "-B", "feature", base)
        for name, text in files.items():
            (origin / name).write_text(text)
            _git(origin, "add", name)
        for old, new in renames:
            _git(origin, "mv", old, new)
        _git(origin, "commit", "-q", "-m", "pr")
        head = _git(origin, "rev-parse", "HEAD")
        _git(origin, "checkout", "-q", "main")
        (origin / "README.md").write_text("base moved on\n")  # base advanced: not the PR's change
        _git(origin, "commit", "-q", "-am", "main moves")
        _git(origin, "checkout", "-q", "--detach", "main")
        _git(origin, "merge", "-q", "--no-ff", "-m", "merge", head)
        merge = _git(origin, "rev-parse", "HEAD")
        _git(origin, "update-ref", "refs/pull/1/merge", merge)
        _git(origin, "checkout", "-q", "main")
        work = tmp_path / "work"
        _git(tmp_path, "clone", "-q", "--depth", "1", "--no-checkout", f"file://{origin}", str(work))
        _git(work, "fetch", "-q", "--depth", "1", "origin", "refs/pull/1/merge")
        _git(work, "checkout", "-q", "--detach", merge)
        return work, head, merge

    return tmp_path, base, make


def _run(tmp_path, work, base, head, merge):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho 'gh: API rate limit exceeded for installation. (HTTP 403)' >&2\nexit 1\n")
    gh.chmod(0o755)
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "sleep").chmod(0o755)
    out = tmp_path / "github_output"
    out.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "EVENT_NAME": "pull_request", "REPO": "owner/repo", "GH_TOKEN": "x",
        "BASE_SHA": base, "HEAD_SHA": head, "GITHUB_SHA": merge,
        "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _script()],
                          cwd=work, env=env, capture_output=True, text=True, encoding="utf-8", check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lanes = dict(line.split("=", 1) for line in out.read_text().split() if "=" in line)
    changed = (tmp_path / "detect-changed-files.txt").read_text().split()
    return proc.stdout, lanes, changed


def test_api_failure_uses_merge_diff_and_skips_unrelated_state_lane(pr):
    tmp_path, base, make = pr
    work, head, merge = make({"tui_gateway_update_pause.py": "z = 1\n"})
    stdout, lanes, changed = _run(tmp_path, work, base, head, merge)
    assert "using the PR merge commit's git diff" in stdout
    assert changed == ["tui_gateway_update_pause.py"]  # base's README move is not the PR's
    assert lanes["hermes_state"] == "false"
    assert lanes["python"] == "true"


def test_state_change_still_selects_the_macos_state_lane(pr):
    tmp_path, base, make = pr
    work, head, merge = make({"hermes_state.py": "x = 2\n"})
    _, lanes, changed = _run(tmp_path, work, base, head, merge)
    assert changed == ["hermes_state.py"]
    assert lanes["hermes_state"] == "true"


def test_rename_counts_under_both_names(pr):
    tmp_path, base, make = pr
    work, head, merge = make({}, renames=[("hermes_state.py", "moved_state.py")])
    _, lanes, changed = _run(tmp_path, work, base, head, merge)
    assert sorted(changed) == ["hermes_state.py", "moved_state.py"]
    assert lanes["hermes_state"] == "true"


@pytest.mark.parametrize("break_it", ["head_sha", "github_sha"])
def test_unverified_checkout_still_fails_open(pr, break_it):
    tmp_path, base, make = pr
    work, head, merge = make({"tui_gateway_update_pause.py": "z = 1\n"})
    if break_it == "head_sha":
        head = base  # merge's second parent is not the event's head
    else:
        merge = base  # HEAD is not the event's merge commit
    stdout, lanes, changed = _run(tmp_path, work, base, head, merge)
    assert "failing open" in stdout
    assert changed == []
    assert lanes["hermes_state"] == "true"
