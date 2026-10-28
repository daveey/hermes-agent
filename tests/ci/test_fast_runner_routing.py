"""Scheduling contracts for the bounded fork fast-check lane.

The allowlist is intentionally conservative: measured short, no-build jobs only.
A new job needs runtime/resource evidence before it can occupy this lane. YAML
is the executable scheduling configuration, not Python source under inspection.
"""
from __future__ import annotations

import pathlib

from ruamel.yaml import YAML

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
FAST = {
    ("ci.yaml", "detect"),
    ("actionlint.yml", "actionlint"),
    ("lint.yml", "ruff-blocking"),
    ("lint.yml", "code-health"),
    ("lint.yml", "windows-footguns"),
    ("case-collision-check.yml", "check-case-collisions"),
    ("infographic-check.yml", "check-no-committed-infographics"),
    ("blob-size-check.yml", "check-blob-size"),
    ("profile-artifact-check.yml", "check-profile-artifacts"),
    ("lazy-deps-guard.yml", "lazy-deps-guard"),
}


def _workflows():
    yaml = YAML(typ="safe")
    return {p.name: yaml.load(p.read_text(encoding="utf-8"))
            for p in WORKFLOWS.glob("*.y*ml")}


def _labels(job):
    value = job.get("runs-on", [])
    if isinstance(value, dict):
        value = value.get("labels", [])
    return [value] if isinstance(value, str) else value


def test_only_bounded_measured_checks_use_fast_lane():
    workflows = _workflows()
    routed = set()
    for wf, data in workflows.items():
        for name, job in data.get("jobs", {}).items():
            labels = _labels(job)
            if "ci-fast" not in labels:
                continue
            routed.add((wf, name))
            assert labels == ["self-hosted", "ci-fast"]
            assert 0 < job["timeout-minutes"] <= 10
            assert "strategy" not in job, "matrix fan-out needs separate admission review"
            assert "container" not in job and "services" not in job
    assert routed == FAST


def test_observers_and_long_work_do_not_compete_for_fast_slots():
    workflows = _workflows()
    for wf, name in [
        ("ci-review-comment.yml", "comment"),
        ("ci.yaml", "all-checks-pass"),
        ("ci.yaml", "ci-timings"),
        ("supply-chain-audit.yml", "aggregate"),
    ]:
        assert workflows[wf]["jobs"][name]["runs-on"] == ["self-hosted", "hermes-hub-poll"]
    for wf in ["tests.yml", "js-tests.yml", "docs-site-checks.yml", "rust-tests.yml", "remote-backends.yml"]:
        for job in workflows[wf]["jobs"].values():
            assert "ci-fast" not in job.get("runs-on", [])
    assert workflows["lint.yml"]["jobs"]["lint-diff"]["runs-on"] == ["self-hosted", "hermes-hub"]


def test_fast_label_is_known_and_checks_cannot_bypass_the_gate():
    workflows = _workflows()
    labels = YAML(typ="safe").load((ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8"))
    assert "ci-fast" in labels["self-hosted-runner"]["labels"]
    gate = workflows["ci.yaml"]["jobs"]["all-checks-pass"]
    assert gate["name"] == "All required checks pass (v2)"
    assert gate["if"] == "${{ !cancelled() }}"
    for wf, name in FAST:
        job = workflows[wf]["jobs"][name]
        assert not job.get("continue-on-error", False)
        if wf == "actionlint.yml":
            # Its independent trigger must still catch an invalid orchestrator.
            assert "pull_request" in workflows[wf]["on"]
        elif wf == "ci.yaml":
            assert name in gate["needs"]
        else:
            callers = {key for key, value in workflows["ci.yaml"]["jobs"].items()
                       if value.get("uses") == f"./.github/workflows/{wf}"}
            assert callers and callers <= set(gate["needs"])


def test_e2e_upgrade_runs_only_on_bwrap_capable_slots():
    # Its probe step refuses to run the updater unsandboxed; a plain hermes-hub slot cannot
    # start bubblewrap (Docker seccomp/AppArmor, masked /proc, host userns restriction).
    # hermes-hub is intentionally absent: the bwrap slots are spot, and that label on spot
    # is what scheduled long jobs onto hosts EC2 stops mid-run.
    workflows = _workflows()
    job = workflows["tests.yml"]["jobs"]["e2e-upgrade"]
    assert _labels(job) == ["self-hosted", "ci-bwrap"]
    assert "hermes-hub" not in _labels(job)
    probe = [s for s in job["steps"] if s.get("name") == "Install bubblewrap"]
    assert probe and "assert BWRAP_OK" in probe[0]["run"]
    labels = YAML(typ="safe").load((ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8"))
    assert "ci-bwrap" in labels["self-hosted-runner"]["labels"]
    for wf, data in workflows.items():
        for name, j in data.get("jobs", {}).items():
            if "ci-bwrap" in _labels(j):
                assert (wf, name) == ("tests.yml", "e2e-upgrade")


def test_e2e_upgrade_has_release_tags_for_n_minus_1():
    # N-1 = `git describe --tags --abbrev=0 --match 'v20[0-9][0-9].*' HEAD~1`. The fork
    # carries no release tags, so without upstream's the git/core shards fail
    # "No names found, cannot describe anything" (run 40658892506, e2e-upgrade (git)).
    steps = _workflows()["tests.yml"]["jobs"]["e2e-upgrade"]["steps"]
    names = [s.get("name") for s in steps]
    fetch = steps[names.index("Fetch upstream release tags")]
    assert names.index("Checkout code") < names.index("Fetch upstream release tags")
    assert names.index("Fetch upstream release tags") < names.index("Run upgrade e2e tests (${{ matrix.shard }})")
    assert "https://github.com/NousResearch/hermes-agent.git" in fetch["run"]
    assert "refs/tags/v20*:refs/tags/v20*" in fetch["run"]
    # The step proves N-1 resolves instead of letting every N-1 test fail on its own.
    assert fetch["run"].rstrip().endswith("git describe --tags --abbrev=0 --match 'v20[0-9][0-9].*' HEAD~1")
