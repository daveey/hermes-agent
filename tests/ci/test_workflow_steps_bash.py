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
