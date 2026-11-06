"""Fork completion jobs must skip cancellation and never await observer slots.

A superseded run's always() gate used to hold its concurrency group (PRs 53/50).
Moving it onto poll slots introduced another dependency: live-comment observers
can await the very completion jobs queued behind them (PR 221). On the heavy
hermes-hub lane they then queued behind full suites (PR 266: 23 min for a 7 s
aggregate), so they run on the bounded ci-fast lane, which no observer uses.
Upstream later folded the timing report and the supply-chain aggregate into
steps (of the gate and of repo-guards.yml), so the gate is the one completion job.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest
from ruamel.yaml import YAML

_YAML = YAML(typ="safe")
ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
HEAVY = "hermes-hub"
POLL = "hermes-hub-poll"
FAST = "ci-fast"
COMPLETION = {
    "ci.yaml": ("all-checks-pass",),
}


def _jobs():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        data = _YAML.load(path.read_text(encoding="utf-8")) or {}
        for name, job in (data.get("jobs") or {}).items():
            if isinstance(job, dict):
                yield path.name, name, job


def _labels(job):
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return {runs_on}
    if isinstance(runs_on, list):
        return {str(x) for x in runs_on}
    return set()


def test_no_always_job_waits_for_a_heavy_hub_runner():
    offenders = [
        f"{wf}:{name}"
        for wf, name, job in _jobs()
        if "always()" in str(job.get("if", "")) and HEAVY in _labels(job)
    ]
    assert not offenders, (
        "job-level always() on hermes-hub keeps cancelled runs queued; "
        f"use !cancelled() on completion jobs: {offenders}"
    )


@pytest.mark.parametrize("wf,name", [
    (wf, name) for wf, names in COMPLETION.items() for name in names
])
def test_aggregators_do_not_wait_for_observers_and_skip_on_cancel(wf, name):
    data = _YAML.load((WORKFLOWS / wf).read_text(encoding="utf-8"))
    job = data["jobs"][name]
    assert _labels(job) == {"self-hosted", FAST}
    assert POLL not in _labels(job) and HEAVY not in _labels(job)
    cond = str(job.get("if", ""))
    assert "!cancelled()" in cond and "always()" not in cond


@pytest.mark.parametrize("original_label", [POLL, HEAVY, FAST, "ubuntu-latest", "blacksmith-4vcpu-ubuntu-2404"])
def test_fork_rewriter_preserves_completion_capacity(tmp_path, original_label):
    # Run the real upstream-reapply entry point twice, without touching the checkout.
    shutil.copytree(ROOT / ".github", tmp_path / ".github")
    for relative in ("scripts/fork-ci-runner.sh", "scripts/ci/required_results.py", "tests/ci/conftest.py"):
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, dest)
    for wf, names in COMPLETION.items():
        path = tmp_path / ".github/workflows" / wf
        text = path.read_text()
        for name in names:
            match = re.search(r"(?m)^  " + re.escape(name) + r":\n(?:(?!  \S).*\n)*", text)
            assert match
            block = match.group(0)
            label = original_label if original_label in ("ubuntu-latest", "blacksmith-4vcpu-ubuntu-2404") else f"[self-hosted, {original_label}]"
            changed = re.sub(r"(?m)^    runs-on: .*$", f"    runs-on: {label}", block)
            changed = re.sub(r"(?m)^    if: .*$", "    if: always()", changed)
            text = text.replace(block, changed)
        path.write_text(text)
    for _ in range(2):
        subprocess.run(["bash", "scripts/fork-ci-runner.sh"], cwd=tmp_path, check=True, capture_output=True)
        for wf, names in COMPLETION.items():
            data = _YAML.load((tmp_path / ".github/workflows" / wf).read_text())
            for name in names:
                job = data["jobs"][name]
                assert _labels(job) == {"self-hosted", FAST}
                assert job["if"] == "${{ !cancelled() }}"
        observer = _YAML.load((tmp_path / ".github/workflows/ci-review-comment.yml").read_text())
        assert _labels(observer["jobs"]["comment"]) == {"self-hosted", POLL}


def test_detect_timeout_tolerates_a_loaded_hub():
    data = _YAML.load((WORKFLOWS / "ci.yaml").read_text(encoding="utf-8"))
    assert data["jobs"]["detect"]["timeout-minutes"] >= 5


def test_poll_label_is_registered_for_actionlint():
    cfg = _YAML.load((ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8"))
    assert POLL in cfg["self-hosted-runner"]["labels"]
