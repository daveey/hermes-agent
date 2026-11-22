"""Workflow-step replay picks a bash 4+, not merely the first bash on PATH.

The replayed CI steps use ``mapfile`` (bash 4+). macOS puts /bin/bash 3.2 first on
PATH, so every replay on a Mac died with ``mapfile: command not found`` (17 false
failures in test_update_ci_routing.py) although Homebrew's bash 5 was installed.
These tests drive ``_run`` itself with a fake bash 3 first on PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ci import workflow_steps

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake bashes are POSIX sh scripts")

# Answers the version probe as bash 3 and fails a script the way bash 3.2 fails mapfile.
FAKE_BASH3 = """#!/bin/sh
case "$*" in
  *BASH_VERSINFO*) echo 3; exit 0 ;;
esac
echo "step.sh: line 1: mapfile: command not found" >&2
exit 127
"""

STEP = {
    "name": "uses mapfile",
    "run": 'mapfile -t lines < <(printf "a\\nb\\n")\n'
           'echo "count=${#lines[@]}" >> "$GITHUB_OUTPUT"\n'
           'echo "major=${BASH_VERSINFO[0]}" >> "$GITHUB_OUTPUT"\n',
}


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _a_real_bash4() -> str | None:
    """Found here, not through workflow_steps: these tests must also run against the old _run."""
    for bash in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if bash and os.access(bash, os.X_OK):
            probe = subprocess.run([bash, "-c", 'echo "${BASH_VERSINFO[0]}"'], capture_output=True,
                                   encoding="utf-8", timeout=30, check=False)
            if probe.stdout.strip().isdigit() and int(probe.stdout) >= 4:
                return bash
    return None


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    """PATH = a dir whose bash is a fake 3.x (git rides along: _run resolves it too)."""
    real = _a_real_bash4()
    if real is None:
        pytest.skip("no bash >= 4 on this host to stand in for Homebrew's")
    old = tmp_path / "old"
    old.mkdir()
    _executable(old / "bash", FAKE_BASH3)
    git = shutil.which("git")
    assert git, "git is needed by the replayed steps"
    (old / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(old))
    return old, real


def test_a_bash4_in_a_known_dir_beats_a_bash3_first_on_path(fake_path, tmp_path, monkeypatch):
    _, real = fake_path
    brew = tmp_path / "brew"
    brew.mkdir()
    new = _executable(brew / "bash", f'#!/bin/sh\nexec "{real}" "$@"\n')
    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (str(tmp_path / "absent"), str(brew)), raising=False)

    outputs = workflow_steps.outputs(STEP, {})
    assert outputs["count"] == "2"
    assert int(outputs["major"]) >= 4
    assert workflow_steps.find_replay_bash() == (str(new), "")


def test_a_bash4_first_on_path_is_kept(fake_path, tmp_path, monkeypatch):
    old, real = fake_path
    first = tmp_path / "first"
    first.mkdir()
    new = _executable(first / "bash", f'#!/bin/sh\nexec "{real}" "$@"\n')
    monkeypatch.setenv("PATH", os.pathsep.join((str(first), str(old))))
    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (str(old),), raising=False)

    assert workflow_steps.outputs(STEP, {})["count"] == "2"
    assert workflow_steps.find_replay_bash() == (str(new), "")


def test_no_bash4_anywhere_skips_naming_the_bash_found(fake_path, tmp_path, monkeypatch):
    old, _ = fake_path
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (str(empty),), raising=False)

    with pytest.raises(pytest.skip.Exception) as skipped:
        workflow_steps.outputs(STEP, {})
    reason = str(skipped.value)
    assert f"{old / 'bash'} (bash 3)" in reason
    assert "bash >= 4" in reason and str(empty) in reason


def test_a_bash_that_does_not_answer_is_not_selected(fake_path, tmp_path, monkeypatch):
    old, _ = fake_path
    _executable(old / "bash", "#!/bin/sh\nexit 1\n")
    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (), raising=False)

    bash, reason = workflow_steps.find_replay_bash()
    assert bash is None
    assert f"{old / 'bash'} (bash unknown version)" in reason


# The replay memo (workflow_steps._REPLAYED) must not outlive the PATH / known dirs it was filled
# under. These tests share one STEP, so any order of them (or of other files' replays of the same
# script in this process) used to hand test_no_bash4_anywhere... a hit from a bash 4 run: the hit
# skipped replay_bash(), so nothing skipped and the test failed whole-file yet passed alone.


@pytest.fixture
def memo(monkeypatch):
    """A private replay memo; the list records the bash of every replay that really spawned."""
    spawned: list[str] = []
    real = workflow_steps._execute

    def counting(*args, **kwargs):
        spawned.append(workflow_steps.replay_bash())
        return real(*args, **kwargs)

    monkeypatch.setattr(workflow_steps, "_REPLAYED", {})
    monkeypatch.setattr(workflow_steps, "_execute", counting)
    return spawned


def _bash4_dir(parent: Path, name: str, real: str) -> tuple[Path, Path]:
    directory = parent / name
    directory.mkdir()
    return directory, _executable(directory / "bash", f'#!/bin/sh\nexec "{real}" "$@"\n')


def _skip_reason() -> str:
    with pytest.raises(pytest.skip.Exception) as skipped:
        workflow_steps.outputs(STEP, {})
    return str(skipped.value)


def test_a_memoized_replay_never_hides_a_path_without_bash4(fake_path, memo, tmp_path, monkeypatch):
    old, real = fake_path
    first, new = _bash4_dir(tmp_path, "first", real)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (str(empty),), raising=False)
    both = os.pathsep.join((str(first), str(old)))
    monkeypatch.setenv("PATH", both)

    assert workflow_steps.outputs(STEP, {})["count"] == "2"  # fills the memo under bash 4
    assert memo == [str(new)]

    monkeypatch.setenv("PATH", str(old))  # same step, env and cwd: only PATH changed
    reason = _skip_reason()
    assert f"{old / 'bash'} (bash 3)" in reason
    assert "bash >= 4" in reason and str(empty) in reason
    assert memo == [str(new)]  # skipped before any spawn

    monkeypatch.setenv("PATH", both)  # back: the first entry is still memoized
    assert workflow_steps.outputs(STEP, {})["count"] == "2"
    assert memo == [str(new)]


def test_a_memoized_replay_never_hides_a_known_dir_change(fake_path, memo, tmp_path, monkeypatch):
    old, real = fake_path
    brew, new = _bash4_dir(tmp_path, "brew", real)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (str(brew),), raising=False)

    assert workflow_steps.outputs(STEP, {})["count"] == "2"  # bash 3 on PATH, bash 4 in a known dir
    assert memo == [str(new)]

    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (str(empty),), raising=False)
    reason = _skip_reason()
    assert f"{old / 'bash'} (bash 3)" in reason
    assert "bash >= 4" in reason and str(empty) in reason and str(brew) not in reason
    assert memo == [str(new)]

    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (str(brew),), raising=False)
    assert workflow_steps.outputs(STEP, {})["count"] == "2"
    assert memo == [str(new)]


def test_the_memo_still_serves_one_bash_and_reselects_another(fake_path, memo, tmp_path, monkeypatch):
    old, real = fake_path
    first, one = _bash4_dir(tmp_path, "one", real)
    second, two = _bash4_dir(tmp_path, "two", real)
    monkeypatch.setattr(workflow_steps, "BASH4_KNOWN_DIRS", (), raising=False)

    monkeypatch.setenv("PATH", os.pathsep.join((str(first), str(old))))
    for _ in range(3):
        assert int(workflow_steps.outputs(STEP, {})["major"]) >= 4
    assert memo == [str(one)]  # three replays, one spawn

    monkeypatch.setenv("PATH", os.pathsep.join((str(second), str(old))))
    workflow_steps.outputs(STEP, {})
    workflow_steps.outputs(STEP, {})
    assert memo == [str(one), str(two)]  # the other bash is its own entry, then memoized too
