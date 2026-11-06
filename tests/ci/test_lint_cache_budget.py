"""Lint validation must survive an optional cache-save stall.

The live blackhole smoke exercises GitHub's step-timeout recovery; these contracts ensure
all lint lanes use that policy and cannot register automatic cache/prune post hooks.
"""
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]


def load(relative):
    return YAML(typ="safe").load((ROOT / relative).read_text(encoding="utf-8"))


# Upstream merged lint-diff, ruff-blocking, code-health and windows-footguns into one `lint` job.
@pytest.mark.parametrize("lane", ["lint"])
def test_lint_cache_is_restore_only_with_a_bounded_best_effort_save(lane):
    job = load(".github/workflows/lint.yml")["jobs"][lane]
    setup = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/setup-pm")
    inputs = setup["with"]
    assert str(inputs["save-tools-cache"]).lower() == "false"
    assert str(inputs["cache-python"]).lower() == "false"
    assert inputs.get("toolchain", "python") == "python", "npm post-save must stay disabled too"
    assert not job.get("continue-on-error", False), "only the optional save may ignore errors"
    save = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/cache/save@"))
    probe = load(".github/workflows/cache-save-timeout-smoke.yml")["jobs"]["stalled-save"]
    stalled_save = next(step for step in probe["steps"] if step.get("id") == "save")
    assert save["uses"] == stalled_save["uses"]
    assert 0 < save["timeout-minutes"] == stalled_save["timeout-minutes"] < job["timeout-minutes"]
    assert save["continue-on-error"] is stalled_save["continue-on-error"] is True
    for output in ("tools-cache-path", "tools-cache-key"):
        assert f"steps.{setup['id']}.outputs.{output}" in " ".join(save["with"].values())
    action = load(".github/actions/setup-pm/action.yml")
    assert "steps.tools-cache-restore.outputs.cache-primary-key" in action["outputs"]["tools-cache-key"]["value"]
    assert "steps.prepare.outputs.store" in action["outputs"]["tools-cache-path"]["value"]
    assert not any(step.get("uses", "").startswith("actions/cache@") for step in job["steps"])
