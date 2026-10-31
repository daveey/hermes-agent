"""Artifact storage is advisory: the account-wide quota never decides a check.

GitHub's artifact storage quota is shared by every repo and runner on the
account. When it is hit, ``upload-artifact`` fails with "Artifact storage quota
has been hit" no matter which runner the job is on, so an upload that can fail
its step turns a green lane red for a reason unrelated to the code. Uploads are
diagnostics only, and the required-checks gate never reads an artifact.
"""
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
REQUIRED_GATE = "All required checks pass (v2)"


def _loaded():
    yaml = YAML(typ="base")
    return {path.name: yaml.load(path.read_text(encoding="utf-8")) for path in sorted(WORKFLOWS.glob("*.y*ml"))}


def _steps(job):
    return job.get("steps") or []


def _uses(step, action):
    return str(step.get("uses", "")).startswith(f"actions/{action}@")


def test_every_artifact_upload_is_continue_on_error():
    blocking = [
        f"{name}:{job_name}:{step.get('name', step.get('uses'))}"
        for name, workflow in _loaded().items()
        for job_name, job in (workflow.get("jobs") or {}).items()
        for step in _steps(job)
        if _uses(step, "upload-artifact") and str(step.get("continue-on-error", "false")).lower() != "true"
    ]
    assert not blocking, f"upload-artifact steps that can fail their job on the artifact quota: {blocking}"


def test_buildkit_cache_exports_are_advisory_without_ignoring_build_failures():
    exporters = [
        (f"{name}:{job_name}:{step.get('name', step.get('uses'))}", step)
        for name, workflow in _loaded().items()
        for job_name, job in (workflow.get("jobs") or {}).items()
        for step in _steps(job)
        if "type=gha" in str((step.get("with") or {}).get("cache-to", ""))
    ]
    assert exporters, "no GitHub Actions cache exporters exercised"
    for name, step in exporters:
        cache_to = step["with"]["cache-to"]
        assert "ignore-error=true" in cache_to, f"optional cache export can fail the build: {name}"
        assert str(step.get("continue-on-error", "false")).lower() == "false", (
            f"real image build/push failures must remain fatal: {name}"
        )


def _required_jobs(workflows):
    """Every job the required gate waits on, following local reusable-workflow calls."""
    jobs = workflows["ci.yaml"]["jobs"]
    (gate,) = [name for name, job in jobs.items() if job.get("name") == REQUIRED_GATE]
    pending, seen = [("ci.yaml", gate)], set()
    while pending:
        file, name = pending.pop()
        if (file, name) in seen:
            continue
        seen.add((file, name))
        job = workflows[file]["jobs"][name]
        needs = job.get("needs", [])
        pending += [(file, need) for need in ([needs] if isinstance(needs, str) else needs)]
        called = str(job.get("uses", ""))
        if called.startswith("./.github/workflows/"):
            callee = called.rsplit("/", 1)[1]
            pending += [(callee, inner) for inner in workflows[callee]["jobs"]]
        yield file, name, job


def test_required_gate_never_reads_an_artifact():
    workflows = _loaded()
    readers = [
        f"{file}:{name}"
        for file, name, job in _required_jobs(workflows)
        if any(_uses(step, "download-artifact") for step in _steps(job))
    ]
    assert not readers, f"required jobs that fail when artifact storage is full: {readers}"
