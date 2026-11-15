#!/usr/bin/env python3
"""Rerun a macOS state job once when its dedicated self-hosted runner was lost.

Called by .github/workflows/hermes-state-recovery.yml on ``workflow_run``
(completed) for CI and hermes_state (macOS). That workflow checks out only its
own trusted revision (``github.workflow_sha``) and runs this file; it never
checks out the failed run's head or downloads its artifacts.

A run is rerun (``POST .../rerun-failed-jobs``, which also reruns the failed
jobs' dependents, so the downstream required aggregates are re-evaluated) only
when ALL of these hold, read from the API, not only from the event payload:

* repository and head repository are exactly ``daveey/hermes``;
* the workflow file is one of the approved paths;
* the run is attempt 1, completed, conclusion ``failure``;
* attempt 1 has exactly one job with the approved macOS job name for that
  workflow, it ran on one of the dedicated runners (``RUNNER_NAMES``), failed,
  no step reports a failure, and every failure annotation on it is GitHub's
  exact runner-loss message;
* every other non-successful job in attempt 1 is a known downstream aggregate
  (which rerun-failed-jobs reruns anyway as a dependent);
* immediately before the POST the run is re-read and is still attempt 1,
  completed, failure (a second event for the same run sees attempt 2 and stops).

Anything else is a skip with a logged reason. Test/assertion failures carry a
``Process completed with exit code N`` annotation and a failed step, so they are
never rerun. Standard library only; the GitHub client shells out to ``gh api``
with an argument vector (no shell), using GH_TOKEN from the workflow env.

Tests: tests/ci/test_recover_macos_runner.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Protocol

TRUSTED_REPOSITORY = "softmax-universe/daveey-hermes"
# The dedicated self-hosted runners of the macOS lanes (label hermes-state):
# the Mac mini's daveey login agent and the Softmax room minis' bbci daemons.
RUNNER_NAMES = frozenset({
    "mini-hermes-state-1",
    "sx-main-room-mini-hermes",
    "sx-heart-room-mini-hermes",
    "sx-conway-mini-hermes",
})
MACOS_JOB = "tests/hermes_state (macOS, mini)"

# Approved workflow file -> exact job name of the macOS lane in that workflow,
# and the jobs downstream of it that may fail only because it failed.
WORKFLOWS: dict[str, dict[str, Any]] = {
    ".github/workflows/hermes-state-macos.yml": {
        "job": MACOS_JOB,
        "downstream": frozenset(),
    },
    ".github/workflows/ci.yaml": {
        # Reusable call: "<caller job name> / <called job name>".
        "job": f"hermes_state (macOS) / {MACOS_JOB}",
        "downstream": frozenset({"All required checks pass (v2)", "macOS state required (v1)"}),
    },
}

# GitHub's annotation text for a lost self-hosted runner, verbatim from run
# 41003441463 (check run 123892862167), read back 2026-11-01.
RUNNER_LOSS_MESSAGES = frozenset({
    "The self-hosted runner lost communication with the server. Verify the machine is "
    "running and has a healthy network connection. Anything in your workflow that "
    "terminates the runner process, starves it for CPU/Memory, or blocks its network "
    "access can cause this error.",
})

_OK_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
_PER_PAGE = 100
_MAX_PAGES = 50


class Api(Protocol):
    def get(self, path: str) -> Any: ...
    def post(self, path: str) -> Any: ...


class ApiError(RuntimeError):
    pass


class GhApi:
    """GitHub REST via ``gh api``; argv only, never a shell."""

    def __init__(self, gh: str = "gh") -> None:
        self.gh = gh

    def _call(self, args: list[str]) -> Any:
        proc = subprocess.run([self.gh, "api", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, timeout=60)
        if proc.returncode != 0:
            raise ApiError(f"gh api {args[-1]} failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def get(self, path: str) -> Any:
        return self._call(["-H", "Accept: application/vnd.github+json", path])

    def post(self, path: str) -> Any:
        return self._call(["-X", "POST", "-H", "Accept: application/vnd.github+json", path])


@dataclass(frozen=True)
class Decision:
    action: str  # "rerun" | "skip"
    reason: str
    run_id: int | None = None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "run_id": self.run_id,
                "dry_run": self.dry_run}


def _skip(reason: str, run_id: int | None = None) -> Decision:
    return Decision("skip", reason, run_id)


def _full_name(obj: Any) -> str | None:
    return obj.get("full_name") if isinstance(obj, dict) else None


def _run_problem(run: Any, repository: str) -> str | None:
    """Why this run is not a first-attempt failed run of an approved workflow."""
    if not isinstance(run, dict):
        return "run payload is not an object"
    if _full_name(run.get("repository")) != repository:
        return f"repository {_full_name(run.get('repository'))!r} is not {repository!r}"
    if _full_name(run.get("head_repository")) != repository:
        return f"head repository {_full_name(run.get('head_repository'))!r} is not {repository!r}"
    if run.get("path") not in WORKFLOWS:
        return f"workflow path {run.get('path')!r} is not approved"
    if run.get("run_attempt") != 1:
        return f"run attempt is {run.get('run_attempt')!r}, not 1"
    if run.get("status") != "completed":
        return f"run status is {run.get('status')!r}, not completed"
    if run.get("conclusion") != "failure":
        return f"run conclusion is {run.get('conclusion')!r}, not failure"
    return None


def _paginate(api: Api, base: str, key: str | None) -> list[Any]:
    items: list[Any] = []
    for page in range(1, _MAX_PAGES + 1):
        body = api.get(f"{base}?per_page={_PER_PAGE}&page={page}")
        chunk = body.get(key, []) if key else body
        if not isinstance(chunk, list):
            raise ApiError(f"{base}: page {page} is not a list")
        items.extend(chunk)
        total = body.get("total_count") if key else None
        if not chunk or len(chunk) < _PER_PAGE or (total is not None and len(items) >= total):
            return items
    raise ApiError(f"{base}: more than {_MAX_PAGES} pages")


def _job_problem(job: dict[str, Any], annotations: list[Any]) -> str | None:
    """Why this macOS job failure is not a pure runner loss."""
    if job.get("run_attempt") != 1:
        return f"job attempt is {job.get('run_attempt')!r}, not 1"
    if job.get("status") != "completed" or job.get("conclusion") != "failure":
        return f"job is {job.get('status')!r}/{job.get('conclusion')!r}, not completed/failure"
    runner = job.get("runner_name")
    if not isinstance(runner, str) or runner not in RUNNER_NAMES:
        return f"job ran on {runner!r}, not a dedicated macOS runner"
    failed_steps = [s.get("name") for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
    if failed_steps:
        return f"step(s) failed: {failed_steps}"
    failures = [a for a in annotations if isinstance(a, dict) and a.get("annotation_level") == "failure"]
    if not failures:
        return "no failure annotation on the job"
    other = [a.get("message") for a in failures if a.get("message") not in RUNNER_LOSS_MESSAGES]
    if other:
        return f"failure annotation is not a runner loss: {other[0]!r}"
    return None


def recover(event: dict[str, Any], api: Api, *, repository: str = TRUSTED_REPOSITORY,
            dry_run: bool = False) -> Decision:
    if repository != TRUSTED_REPOSITORY:
        return _skip(f"repository {repository!r} is not {TRUSTED_REPOSITORY!r}")
    if event.get("action") != "completed":
        return _skip(f"event action {event.get('action')!r} is not completed")
    payload_run = event.get("workflow_run")
    problem = _run_problem(payload_run, repository)
    if problem or not isinstance(payload_run, dict):
        return _skip(f"event: {problem}")
    run_id = payload_run.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        return _skip(f"run id {run_id!r} is not a positive integer")

    run_path = f"repos/{repository}/actions/runs/{run_id}"
    run = api.get(run_path)
    problem = _run_problem(run, repository) or (
        None if run.get("id") == run_id else f"API returned run {run.get('id')!r}")
    if problem:
        return _skip(f"api: {problem}", run_id)
    if run.get("path") != payload_run.get("path"):
        return _skip("api workflow path differs from the event", run_id)
    spec = WORKFLOWS[run["path"]]

    jobs = _paginate(api, f"{run_path}/attempts/1/jobs", "jobs")
    targets = [j for j in jobs if j.get("name") == spec["job"]]
    if len(targets) != 1:
        return _skip(f"expected exactly one {spec['job']!r} job, found {len(targets)}", run_id)
    target = targets[0]
    for job in jobs:
        if job is target:
            continue
        if job.get("status") != "completed":
            return _skip(f"job {job.get('name')!r} is {job.get('status')!r}", run_id)
        if job.get("conclusion") not in _OK_CONCLUSIONS and job.get("name") not in spec["downstream"]:
            return _skip(f"other job {job.get('name')!r} concluded {job.get('conclusion')!r}", run_id)
    if not isinstance(target.get("id"), int):
        return _skip("macOS job id is not an integer", run_id)
    annotations = _paginate(api, f"repos/{repository}/check-runs/{target['id']}/annotations", None)
    problem = _job_problem(target, annotations)
    if problem:
        return _skip(f"macOS job: {problem}", run_id)

    # Duplicate safety: another event (or a person) may have rerun it already.
    problem = _run_problem(api.get(run_path), repository)
    if problem:
        return _skip(f"re-read before rerun: {problem}", run_id)
    # An old PR rerun joins CI's per-PR concurrency group and could cancel the
    # current head. Refuse obsolete or unidentifiable PRs before mutation.
    if run.get("event") == "pull_request":
        pulls = run.get("pull_requests") or []
        if len(pulls) != 1 or not isinstance(pulls[0].get("number"), int):
            return _skip("PR identity is missing or ambiguous", run_id)
        pull = api.get(f"repos/{repository}/pulls/{pulls[0]['number']}")
        if pull.get("state") != "open" or (pull.get("head") or {}).get("sha") != run.get("head_sha"):
            return _skip("PR closed or head superseded", run_id)
    reason = f"runner lost on {target['runner_name']}; rerunning failed jobs of attempt 1"
    if dry_run:
        return Decision("rerun", reason, run_id, dry_run=True)
    api.post(f"{run_path}/rerun-failed-jobs")
    return Decision("rerun", reason, run_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"),
                        help="workflow_run event JSON (default: $GITHUB_EVENT_PATH)")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dry-run", action="store_true", help="decide but do not POST")
    args = parser.parse_args(argv)
    if not args.event:
        parser.error("--event or GITHUB_EVENT_PATH is required")
    with open(args.event, encoding="utf-8-sig") as fh:
        event = json.load(fh)
    try:
        decision = recover(event, GhApi(), repository=args.repository, dry_run=args.dry_run)
    except (ApiError, json.JSONDecodeError) as exc:
        print(json.dumps({"action": "error", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(decision.as_dict()))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"Runner-loss recovery: **{decision.action}** - {decision.reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
