"""Scheduling contracts for the bounded fork fast-check lane.

The allowlist is intentionally conservative: measured short, no-build jobs only.
A new job needs runtime/resource evidence before it can occupy this lane. YAML
is the executable scheduling configuration, not Python source under inspection.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest
from ruamel.yaml import YAML

pytestmark = pytest.mark.fork_guard

ROOT = pathlib.Path(__file__).resolve().parents[2]
# Blacksmith sizes since 2026-11-11 (softmax-universe): the light lane is 2 vCPU, everything else 4
# (blacksmith-8vcpu jobs sat queued with no runner on the org, PR #1077).
FAST_LABEL = "blacksmith-2vcpu-ubuntu-2404"
HEAVY_LABEL = "blacksmith-4vcpu-ubuntu-2404"
BIG_LABEL = "blacksmith-4vcpu-ubuntu-2404"
POLL_LABEL = "blacksmith-2vcpu-ubuntu-2404"
WORKFLOWS = ROOT / ".github" / "workflows"
FAST = {
    ("ci.yaml", "detect"),
    ("actionlint.yml", "actionlint"),
    # Second admission (October 30, 2026): completion aggregates. Each needs only
    # git and python3, and ran 7-62 s median on hub runners while it queued
    # 7-26 min behind full suites (.github/FAST-LANE.md).
    ("ci.yaml", "all-checks-pass"),
    # Upstream consolidation (November 2026) removed the other admitted jobs as jobs:
    # the CI timing report became advisory trailing steps of all-checks-pass; ruff
    # enforcement, code health and Windows footguns became steps of lint.yml's one
    # `lint` job (20 min, with the full-tree ruff + ty diff, so heavy lane); and every
    # repository guard and supply-chain job became steps of repo-guards.yml's `guards`
    # (15 min, `docker run` hadolint, so docker-ci). Neither combined job is light.
    # Third admission (November 1, 2026): the macOS state lane's own required
    # check. Checkout plus one python3 call over toJSON(needs); 14 s on ci-fast
    # in run 41326127097 (job 124910774212). Its always() must stay (fail-closed
    # when the lane is skipped or cancelled), and an always() job may not wait
    # on hermes-hub (test_fork_runner_routing), so it cannot go back there.
    ("ci.yaml", "macos-state-required"),
    # Fourth admission (November 11, 2026, Blacksmith cutover): the Docker release-phase gate.
    # It only reads its needs' results (5-min cap, no checkout or build); its always()
    # may not wait on the heavy size (test_fork_runner_routing).
    ("docker.yml", "release-phase-gate"),
}
# Completion job in the orchestrator itself: the gate (the advisory timing report
# runs as its trailing steps). It cannot be one of its own needs.
COMPLETION = {("ci.yaml", "all-checks-pass")}
# Consolidated check jobs: off the fast lane, on the lane their steps need, and
# required through the gate like the separate jobs they replaced.
COMBINED = {
    ("lint.yml", "lint"): HEAVY_LABEL,
    ("repo-guards.yml", "guards"): BIG_LABEL,
}
# Required checks that the ruleset names directly instead of through the gate,
# so they are outside its needs and keep always(): never !cancelled() or skip.
OBSERVERS = {("ci-review-comment.yml", "comment")}
SEPARATE_REQUIRED = {
    ("ci.yaml", "macos-state-required"): ("macOS state required (v1)", {"detect", "hermes-state-macos"}),
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


def _docker_calls(steps, tmp_path, root=ROOT):
    """Run each hadolint step's script as the runner would, with `docker` recorded, not run.

    The step text alone is not the behaviour: upstream moved the `docker run` into
    scripts/ci/lint_docker.sh, so the contract follows the command the step executes.
    """
    hadolint = [s for s in steps if "run" in s and "hadolint" in f"{s.get('name', '')} {s['run']}"]
    assert hadolint, "no hadolint step in the guards job"
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    calls = tmp_path / "docker-calls.tsv"
    docker = stub / "docker"
    docker.write_text('#!/usr/bin/env bash\nprintf \'%s\\t\' "$@" >> "$DOCKER_CALLS"\necho >> "$DOCKER_CALLS"\n',
                      encoding="utf-8")
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}", "DOCKER_CALLS": str(calls)}
    for step in hadolint:
        assert "${{" not in step["run"], "expressions in the step need rendering before replay"
        # GitHub's default Linux shell for `run:` is `bash --noprofile --norc -eo pipefail {0}`.
        done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                              cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False)
        assert done.returncode == 0, (step.get("name"), done.stdout, done.stderr)
    if not calls.exists():
        return []
    return [line.split("\t")[:-1] for line in calls.read_text(encoding="utf-8").splitlines()]


def test_only_bounded_measured_checks_use_fast_lane():
    workflows = _workflows()
    routed = set()
    for wf, data in workflows.items():
        for name, job in data.get("jobs", {}).items():
            labels = _labels(job)
            if labels != [FAST_LABEL] or (wf, name) in OBSERVERS:
                continue
            routed.add((wf, name))
            assert 0 < job["timeout-minutes"] <= 10
            assert "strategy" not in job, "matrix fan-out needs separate admission review"
            assert "container" not in job and "services" not in job
    assert routed == FAST


def test_observers_and_long_work_do_not_compete_for_fast_slots(tmp_path):
    workflows = _workflows()
    for wf, name in [
        ("ci-review-comment.yml", "comment"),
    ]:
        assert workflows[wf]["jobs"][name]["runs-on"] == POLL_LABEL
    for wf in ["tests.yml", "js-tests.yml", "docs-site-checks.yml", "rust-tests.yml", "remote-backends.yml"]:
        for job in workflows[wf]["jobs"].values():
            assert job.get("runs-on") != FAST_LABEL
    for (wf, name), labels in COMBINED.items():
        assert workflows[wf]["jobs"][name]["runs-on"] == labels, (wf, name)
    # The combined guards job runs hadolint via `docker run` (8 vCPU Docker lane).
    guards = workflows["repo-guards.yml"]["jobs"]["guards"]["steps"]
    assert ["run"] in [call[:1] for call in _docker_calls(guards, tmp_path)]
    # Test suites and E2E stay on the heavy lane even when one run is short.
    for wf, name in [
        ("catalog-tests.yml", "catalog-tests"),
        ("bootstrap-installer.yml", "posix"),
        ("tests.yml", "e2e-upgrade-plan"),
        ("termux-verify.yml", "contracts"),
    ]:
        assert workflows[wf]["jobs"][name]["runs-on"] == HEAVY_LABEL


_HADOLINT_STEP = {"name": "Lint Dockerfiles (hadolint)", "run": "scripts/ci/lint_docker.sh hadolint"}


@pytest.mark.parametrize(
    ("hadolint_body", "steps", "docker_run"),
    [
        # The real script's shape: the step runs a script whose function runs docker.
        ('run_hadolint() { docker run --rm img hadolint Dockerfile; }', [_HADOLINT_STEP], True),
        # Through one more level (a just-style wrapper script): still docker.
        ('run_hadolint() { bash scripts/ci/inner.sh; }', [_HADOLINT_STEP], True),
        # Broken: the script calls a hadolint binary (no daemon needed, so docker-ci is moot).
        ("run_hadolint() { true hadolint Dockerfile; }", [_HADOLINT_STEP], False),
        # Broken: docker only pulls, never runs.
        ("run_hadolint() { docker pull img; }", [_HADOLINT_STEP], False),
        # Broken: the step no longer calls the script.
        ('run_hadolint() { docker run img; }', [{"name": "Lint Dockerfiles (hadolint)", "run": "true"}], False),
    ],
)
def test_docker_run_contract_follows_the_script(tmp_path, hadolint_body, steps, docker_run):
    """Negative controls: the docker-ci contract reads the executed script, not the step text."""
    root = tmp_path / "repo"
    (root / "scripts/ci").mkdir(parents=True)
    script = root / "scripts/ci/lint_docker.sh"
    script.write_text(f'#!/usr/bin/env bash\nset -euo pipefail\n{hadolint_body}\n'
                      'case "$1" in hadolint) run_hadolint ;; *) exit 2 ;; esac\n', encoding="utf-8")
    (root / "scripts/ci/inner.sh").write_text("docker run --rm img hadolint\n", encoding="utf-8")
    script.chmod(0o755)
    calls = _docker_calls(steps, tmp_path, root)
    assert (["run"] in [call[:1] for call in calls]) is docker_run, calls


def test_a_failing_hadolint_step_fails_the_contract(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts/ci").mkdir(parents=True)
    script = root / "scripts/ci/lint_docker.sh"
    script.write_text("#!/usr/bin/env bash\ndocker run img\nexit 3\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(AssertionError):
        _docker_calls([_HADOLINT_STEP], tmp_path, root)
    with pytest.raises(AssertionError, match="no hadolint step"):
        _docker_calls([{"name": "other", "run": "docker run x"}], tmp_path, root)


def test_fast_label_is_known_and_checks_cannot_bypass_the_gate():
    workflows = _workflows()
    labels = YAML(typ="safe").load((ROOT / ".github" / "actionlint.yaml").read_text(encoding="utf-8"))
    assert FAST_LABEL in labels["self-hosted-runner"]["labels"]
    gate = workflows["ci.yaml"]["jobs"]["all-checks-pass"]
    assert gate["name"] == "All required checks pass (v2)"
    assert gate["if"] == "${{ !cancelled() }}"
    for wf, name in FAST:
        job = workflows[wf]["jobs"][name]
        assert not job.get("continue-on-error", False)
        if wf == "actionlint.yml":
            # Its independent trigger must still catch an invalid orchestrator.
            assert "pull_request" in workflows[wf]["on"]
        elif (wf, name) in COMPLETION:
            assert name == "all-checks-pass" or "all-checks-pass" in job["needs"]
            assert job["if"] == "${{ !cancelled() }}"
        elif (wf, name) in SEPARATE_REQUIRED:
            title, needs = SEPARATE_REQUIRED[(wf, name)]
            assert job["name"] == title
            assert set(job["needs"]) == needs
            assert job["if"] == "${{ always() }}"
        elif (wf, name) == ("docker.yml", "release-phase-gate"):
            # The release orchestrator's phase check, not part of the PR gate.
            assert "always()" in job["if"] and "inputs.release-phase" in job["if"]
        elif wf == "ci.yaml":
            assert name in gate["needs"]
        else:
            callers = {key for key, value in workflows["ci.yaml"]["jobs"].items()
                       if value.get("uses") == f"./.github/workflows/{wf}"}
            assert callers and callers <= set(gate["needs"])
    # The consolidated check jobs stay required through the gate.
    for wf, _ in COMBINED:
        callers = {key for key, value in workflows["ci.yaml"]["jobs"].items()
                   if value.get("uses") == f"./.github/workflows/{wf}"}
        assert callers and callers <= set(gate["needs"]), wf
        assert not any(job.get("continue-on-error", False) for job in workflows[wf]["jobs"].values()), wf
    # The timing report (formerly its own ci-timings job) rides in the gate job: every step after
    # the verdict is advisory, so it can neither fail nor skip the required check.
    steps = gate["steps"]
    verdict = next(i for i, step in enumerate(steps) if step.get("name") == "Evaluate job results")
    assert "continue-on-error" not in steps[verdict]
    report = steps[verdict + 1:]
    assert any("timings_report.py" in step.get("run", "") for step in report)
    for step in report:
        assert step.get("continue-on-error") is True, step.get("name")
        assert "always()" in str(step.get("if", "")), step.get("name")


@pytest.mark.parametrize("upstream_label", ["ubuntu-latest", "blacksmith-4vcpu-ubuntu-2404", "[self-hosted, hermes-hub]", "[self-hosted, ci-fast]"])
def test_fork_rewriter_keeps_every_fast_job_on_the_fast_lane(tmp_path, upstream_label):
    # An upstream merge brings back ubuntu-latest/Blacksmith (or an older fork commit's
    # hermes-hub) on these lines; the reapply script must pin them back.
    shutil.copytree(ROOT / ".github", tmp_path / ".github")
    for relative in ("scripts/fork-ci-runner.sh", "scripts/ci/required_results.py", "tests/ci/conftest.py"):
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, dest)
    for wf in {wf for wf, _ in FAST} | {wf for wf, _ in COMBINED}:
        path = tmp_path / ".github/workflows" / wf
        text = path.read_text(encoding="utf-8")
        for name in [n for w, n in FAST | set(COMBINED) if w == wf]:
            match = re.search(r"(?m)^  " + re.escape(name) + r":\n(?:(?!  \S).*\n)*", text)
            assert match, (wf, name)
            block = match.group(0)
            changed = re.sub(r"(?m)^    runs-on: .*$", f"    runs-on: {upstream_label}", block)
            if (wf, name) in SEPARATE_REQUIRED:
                # The bare form is what the script's completion rewrite turns into !cancelled().
                changed = re.sub(r"(?m)^    if: .*$", "    if: always()", changed)
            text = text.replace(block, changed)
        path.write_text(text, encoding="utf-8")
    yaml = YAML(typ="safe")
    for _ in range(2):
        subprocess.run(["bash", "scripts/fork-ci-runner.sh"], cwd=tmp_path, check=True, capture_output=True)
        for wf, name in FAST:
            job = yaml.load((tmp_path / ".github/workflows" / wf).read_text(encoding="utf-8"))["jobs"][name]
            assert _labels(job) == [FAST_LABEL], (wf, name)
            if (wf, name) in SEPARATE_REQUIRED:
                # The fast pin must not turn a fail-closed check into a skippable one.
                assert job["if"] == "always()", (wf, name)
    # Heavy and Docker work the script routes is untouched by the fast-lane pin.
    for (wf, name), labels in COMBINED.items():
        job = yaml.load((tmp_path / ".github/workflows" / wf).read_text(encoding="utf-8"))["jobs"][name]
        assert job["runs-on"] == labels, (wf, name)


def test_e2e_upgrade_runs_on_8vcpu_and_lifts_the_userns_restriction():
    # Its probe step refuses to run the updater unsandboxed; bubblewrap needs Ubuntu 24.04's
    # AppArmor userns restriction lifted, which the job does itself on its Blacksmith VM.
    job = _workflows()["tests.yml"]["jobs"]["e2e-upgrade"]
    assert _labels(job) == [BIG_LABEL]
    probe = [s for s in job["steps"] if s.get("name") == "Install bubblewrap"]
    assert probe and "assert BWRAP_OK" in probe[0]["run"]
    assert "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in probe[0]["run"]


def test_plugin_catalog_sandbox_lifts_userns_then_proves_isolation():
    # Plugin code in pinned-source-validate runs only inside bubblewrap. On Blacksmith
    # the job installs bwrap and lifts the userns restriction in ONE named step, before
    # the proof; every other step stays free of host setup, and the proof fails closed.
    job = _workflows()["plugin-catalog-ci.yml"]["jobs"]["pinned-source-validate"]
    assert _labels(job) == [HEAVY_LABEL]
    names = [s.get("name") for s in job["steps"]]
    setup = job["steps"][names.index("Allow bubblewrap user namespaces")]
    proof = job["steps"][names.index("Prove the plugin validation sandbox")]
    assert names.index("Allow bubblewrap user namespaces") < names.index("Prove the plugin validation sandbox")
    assert names.index("Prove the plugin validation sandbox") < names.index("Clone each entry at its pinned sha and validate")
    assert "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in setup["run"]
    assert "|| true" not in setup["run"]
    assert setup["if"] == proof["if"] == "steps.changed.outputs.files != ''"
    assert "python3 -I scripts/ci/plugin_validation_sandbox.py --no-network -- /bin/true" in proof["run"]
    assert "set -euo pipefail" in proof["run"] and "|| true" not in proof["run"]
    for step in job["steps"]:
        if step is not setup:
            assert not re.search(r"\b(sudo|sysctl -w|apt-get)\b", step.get("run", "")), step.get("name")


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
    assert "refs/tags/v0.*:refs/tags/v0.*" in fetch["run"]
    # A reachable calver release must not suppress fetching the newer by-path line.
    assert "if ! git describe" not in fetch["run"]
    assert "--match 'v0.*'" in fetch["run"]
    assert "--exclude '*+*'" in fetch["run"]


def test_termux_assembly_permissions_run_with_docker_and_fail_on_skip():
    # The file's two parametrizations need a docker daemon (Blacksmith VMs have one);
    # --fail-on-skip keeps a daemon-less host from reporting green.
    job = _workflows()["tests.yml"]["jobs"]["termux-assembly-permissions"]
    assert _labels(job) == [HEAVY_LABEL]
    steps = job["steps"]
    names = [s.get("name") for s in steps]
    pull = steps[names.index("Pull the native Linux test image")]
    run = steps[names.index("Run assembly permission tests")]
    assert names.index("Pull the native Linux test image") < names.index("Run assembly permission tests")
    assert "ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a" in pull["with"]["command"]
    assert "--fail-on-skip" in run["run"]
    assert "tests/scripts/test_termux_assembly_permissions.py" in run["run"]
    assert steps[-1].get("uses") == "./.github/actions/check-clean-worktree"


def test_python_slices_match_their_matrix():
    # The slice count is the matrix size: HERMES_TEST_SLICE and the job name read strategy.job-total,
    # so a planner that sizes the matrix can never skip or double-run files.
    wf = _workflows()["tests.yml"]
    job = wf["jobs"]["test"]
    assert job["strategy"]["matrix"]["slice"] == "${{ fromJSON(inputs.python_slices) }}"
    default = wf["on"]["workflow_call"]["inputs"]["python_slices"]["default"]
    assert default == "[1, 2, 3, 4, 5, 6, 7, 8]"
    # The planner's full-suite slice count is the same matrix (scripts/ci/affected_tests.py).
    planner = (ROOT / "scripts" / "ci" / "affected_tests.py").read_text(encoding="utf-8")
    assert "MAX_SLICES = 8 " in planner
    run = next(step for step in job["steps"] if step.get("name") == "Run tests")
    assert run["env"]["HERMES_TEST_SLICE"] == "${{ matrix.slice }}/${{ strategy.job-total }}"
    assert "${{ matrix.slice }}/${{ strategy.job-total }}" in job["name"]
    # One 8 vCPU Blacksmith VM per slice: a slice never shares a CPU budget with other jobs.
    assert job["runs-on"] == BIG_LABEL


def test_pull_requests_pass_their_affected_plan_to_the_slices():
    ci = _workflows()["ci.yaml"]["jobs"]
    outputs = ci["detect"]["outputs"]
    assert outputs["python_changed"] == "${{ steps.python-plan.outputs.changed }}"
    assert outputs["python_slices"] == "${{ steps.python-plan.outputs.slices }}"
    plan = next(step for step in ci["detect"]["steps"] if step.get("id") == "python-plan")
    script = plan["run"]
    # Only a pull request with a usable changed-file list narrows the run.
    for full in ('"$EVENT_NAME" != "pull_request"', '"$FULL_LABEL" = "true"', '-eq 0', '-ge 300'):
        assert full in script
    assert ci["tests"]["with"]["python_changed"] == "${{ needs.detect.outputs.python_changed }}"
    assert ci["tests"]["with"]["python_slices"] == "${{ needs.detect.outputs.python_slices }}"
    run = next(step for step in _workflows()["tests.yml"]["jobs"]["test"]["steps"] if step.get("name") == "Run tests")
    assert run["env"]["PYTHON_CHANGED"] == "${{ inputs.python_changed }}"
    assert "affected_tests.py --changed" in run["run"] and "--files-from" in run["run"]
