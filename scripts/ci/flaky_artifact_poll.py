"""Assemble one run attempt's flaky entries from its metadata artifacts (hub cron side).

``assess_run`` pages the attempt's jobs and the run's artifacts, requires exactly one unexpired
``flaky-metadata-v1-<run>-<attempt>-<slice>`` artifact per expected Python test job, downloads and
validates ALL of them, and only then returns entries. Nothing here touches the Kanban board, so a bad
artifact can never cause a partial card side effect. Failures are fixed codes, never payload or
exception text.

Unavailable data stays unprocessed and keeps the cron non-green throughout the lookback window.
Even an interrupted attempt may contain flakes in a completed slice; never mistake it for no flakes.
"""
from __future__ import annotations

import http.client
import re
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from scripts.ci import flaky_artifact as fa
except ImportError:  # run as a script: scripts/ci is sys.path[0]
    import flaky_artifact as fa  # type: ignore[no-redef]

MAX_PAGES = 100
MAX_RUN_FLAKY = 200
PER_PAGE = 100
_JOB_SUFFIX = re.compile(r"Run tests \((?:affected, )?([1-9][0-9]*)/([1-9][0-9]*)\)", re.ASCII)
_FINISHED = {"success", "failure"}


class Unavailable(Exception):
    """The run's flake data cannot be read; ``code`` is a fixed receipt code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class RunResult:
    kind: str                       # complete | not_applicable
    jobs: list[tuple[int, list[dict[str, str]]]] = field(default_factory=list)   # (job id, entries) by slice


FetchJson = Callable[[str], Any]


def _pages(fetch: FetchJson, url: str, key: str, code: str) -> list[dict[str, Any]]:
    """Every item of a paged GitHub list (up to MAX_PAGES full pages; overflow is rejected)."""
    items: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            body = fetch(f"{url}{'&' if '?' in url else '?'}per_page={PER_PAGE}&page={page}")
        except (OSError, ValueError, http.client.HTTPException):
            raise Unavailable(code) from None
        batch = body.get(key) if isinstance(body, dict) else None
        if not isinstance(batch, list) or len(batch) > PER_PAGE or not all(isinstance(i, dict) for i in batch):
            raise Unavailable(code)
        items += batch
        if len(batch) < PER_PAGE:
            return items
    raise Unavailable(code)


def _job_slice(job: dict[str, Any]) -> tuple[int, int] | None:
    """``(slice, slices)`` of a non-skipped Python test job; None when it is not a test job or was skipped."""
    name = job.get("name")
    if not isinstance(name, str) or "Run tests" not in name or job.get("conclusion") == "skipped":
        return None
    match = _JOB_SUFFIX.search(name)
    if match is None or match.end() != len(name):
        raise Unavailable("jobs_unavailable")
    slice_no, slices = int(match.group(1)), int(match.group(2))
    if not (slice_no <= slices <= fa.MAX_SLICES and job.get("status") == "completed"
            and fa._int_in(job.get("id"), 1, fa.MAX_INT)):
        raise Unavailable("jobs_unavailable")
    if job.get("conclusion") not in _FINISHED:      # cancelled, timed_out, ...: its upload never ran
        raise Unavailable("jobs_unavailable")
    return slice_no, slices


def _job_attempt(job: dict[str, Any], attempt: int) -> int:
    """Only jobs produced by this exact attempt may supply its metadata."""
    own = job.get("run_attempt", attempt)
    if not fa._int_in(own, 1, fa.MAX_INT) or own != attempt:
        raise Unavailable("jobs_unavailable")
    return attempt


def expected_jobs(jobs: list[dict[str, Any]], attempt: int = 1, *,
                  cancelled: bool = False) -> tuple[int, dict[int, tuple[int, int]]]:
    """``(slices, {slice: (job id, producing attempt)})``; no Python jobs means not applicable."""
    if cancelled:
        raise Unavailable("jobs_unavailable")
    by_slice: dict[int, tuple[int, int]] = {}
    totals: set[int] = set()
    for job in jobs:
        found = _job_slice(job)
        if found is None:
            continue
        if found[0] in by_slice:
            raise Unavailable("jobs_unavailable")
        by_slice[found[0]] = (job["id"], _job_attempt(job, attempt))
        totals.add(found[1])
    if not by_slice:
        return 0, {}
    if len(totals) != 1 or set(by_slice) != set(range(1, next(iter(totals)) + 1)):
        raise Unavailable("jobs_unavailable")
    return totals.pop(), by_slice


def _artifact_id(artifacts: list[dict[str, Any]], name: str) -> int:
    """The one unexpired artifact called ``name``; missing/expired/ambiguous are fixed codes."""
    named = [a for a in artifacts if a.get("name") == name]
    live = [a for a in named if a.get("expired") is False]
    if not live:
        raise Unavailable("expired" if named else "missing")
    if len(live) != 1 or not fa._int_in(live[0].get("id"), 1, fa.MAX_INT):
        raise Unavailable("invalid")
    return live[0]["id"]


def _entries(meta: dict[str, Any]) -> list[dict[str, str]]:
    return [{"test_id": e["test_id"], "retried": e["retried"], "first_attempt_excerpt": ""} for e in meta["flaky"]]


def assess_run(repo: str, run: dict[str, Any], fetch: FetchJson, token: str) -> RunResult:
    """Validated entries for every slice of this run attempt, or ``Unavailable``."""
    run_id, attempt, head_sha = run.get("id"), run.get("run_attempt", 1), run.get("head_sha")
    if not (isinstance(repo, str) and fa._REPO.fullmatch(repo)
            and fa._int_in(run_id, 1, fa.MAX_INT) and fa._int_in(attempt, 1, fa.MAX_INT)
            and isinstance(head_sha, str) and fa._SHA.fullmatch(head_sha)):
        raise Unavailable("invalid")
    api = f"https://api.github.com/repos/{repo}"
    jobs = _pages(fetch, f"{api}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs", "jobs_unavailable")
    slices, job_ids = expected_jobs(jobs, attempt, cancelled=run.get("conclusion") == "cancelled")
    if not job_ids:
        return RunResult("not_applicable")
    if not (isinstance(head_sha, str) and fa._SHA.fullmatch(head_sha)):
        raise Unavailable("invalid")
    artifacts = _pages(fetch, f"{api}/actions/runs/{run_id}/artifacts", "artifacts", "artifacts_unavailable")
    wanted = {s: _artifact_id(artifacts, fa.artifact_name(run_id, made, s)) for s, (_, made) in job_ids.items()}
    result = RunResult("complete")
    for slice_no in sorted(job_ids):
        try:
            meta = fa.decode_zip(fa.download_zip(repo, wanted[slice_no], token))
            fa.check_expected(meta, repo=repo, run_id=run_id, attempt=job_ids[slice_no][1], head_sha=head_sha,
                              slice_no=slice_no, slices=slices)
        except fa.ArtifactError as exc:
            raise Unavailable(exc.code) from None
        result.jobs.append((job_ids[slice_no][0], _entries(meta)))
        # PR-controlled artifacts must not turn one run into an unbounded card flood.
        if sum(len(entries) for _, entries in result.jobs) > MAX_RUN_FLAKY:
            raise Unavailable("invalid")
    return result
