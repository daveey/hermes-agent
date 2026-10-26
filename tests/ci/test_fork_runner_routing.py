"""[daveey] fork routing invariants for the self-hosted hermes-hub runners.

A job-level ``if: always()`` on a heavy ``[self-hosted, hermes-hub]`` runner
runs even after its run is CANCELLED, so a superseded PR run sits ``queued``
until a busy test runner frees up, holding the ``ci-<PR>`` concurrency group
and leaving the new head ``pending`` (PR #53, PR #50). Aggregators must use
``!cancelled()`` and the cheap poll-only runners instead.
"""
from __future__ import annotations

import pathlib

import pytest
from ruamel.yaml import YAML

_YAML = YAML(typ="safe")

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
HEAVY = "hermes-hub"
POLL = "hermes-hub-poll"


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
        "job-level always() on [self-hosted, hermes-hub] keeps cancelled runs queued; "
        f"use `${{{{ !cancelled() }}}}` + [self-hosted, {POLL}]: {offenders}")


@pytest.mark.parametrize("wf,name", [
    ("ci.yaml", "all-checks-pass"),
    ("ci.yaml", "ci-timings"),
    ("supply-chain-audit.yml", "aggregate"),
])
def test_aggregators_run_on_poll_runners_and_skip_on_cancel(wf, name):
    data = _YAML.load((WORKFLOWS / wf).read_text(encoding="utf-8"))
    job = data["jobs"][name]
    assert _labels(job) == {"self-hosted", POLL}
    cond = str(job.get("if", ""))
    assert "!cancelled()" in cond and "always()" not in cond


def test_detect_timeout_tolerates_a_loaded_hub():
    data = _YAML.load((WORKFLOWS / "ci.yaml").read_text(encoding="utf-8"))
    assert data["jobs"]["detect"]["timeout-minutes"] >= 5


def test_poll_label_is_registered_for_actionlint():
    cfg = _YAML.load((ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8"))
    assert POLL in cfg["self-hosted-runner"]["labels"]
