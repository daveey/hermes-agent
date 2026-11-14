"""Assemble one run attempt's flaky entries from its metadata artifacts (hub cron side).

``assess_run`` pages the attempt's jobs and the run's artifacts, requires exactly one unexpired
``flaky-metadata-v1-<run>-<attempt>-<slice>`` artifact per expected Python test job, downloads and
validates ALL of them, and only then returns entries. Nothing here touches the Kanban board, so a bad
artifact can never cause a partial card side effect. Failures are fixed codes, never payload or
exception text.

Unavailable data stays unprocessed (the caller retries it next poll and turns the cron red only once it
persists). Even an interrupted attempt may contain flakes in a completed slice; never mistake it for no flakes,
except when a newer run of the same workflow on the same branch superseded the cancelled one (a concurrency
cancel): that newer run tests the branch, so the cancelled one is ``not_applicable``.

A "Re-run failed jobs" attempt copies every job that passed into the new attempt: a new job id stamped with the
new ``run_attempt``, but the original ``started_at``/``completed_at``, and no new upload. Such a carried-over job
is matched to the earlier attempt's job (same name and timestamps), and that attempt's artifact supplies it.
"""
from __future__ import annotations

import http.client
import re
import urllib.parse
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
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)
SUPERSEDE_PAGE = 20     # newest runs of (workflow, branch) read to find a newer one; the list is newest-first


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
    by_slice: dict[int, tuple[int, int]] = {}
    totals: set[int] = set()
    for job in jobs:
        # Even skipped test jobs in a cancelled attempt cannot prove a full matrix.
        name = job.get("name")
        if cancelled and isinstance(name, str) and "Run tests" in name:
            raise Unavailable("jobs_unavailable")
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


def superseded(fetch: FetchJson, api: str, run: dict[str, Any]) -> bool:
    """A newer run of the same workflow on the same branch exists (any status). Unreadable means no."""
    workflow, branch, run_id = run.get("workflow_id"), run.get("head_branch"), run.get("id")
    if not (fa._int_in(workflow, 1, fa.MAX_INT) and isinstance(branch, str) and 0 < len(branch) <= 255):
        return False
    url = (f"{api}/actions/workflows/{workflow}/runs?branch={urllib.parse.quote(branch, safe='')}"
           f"&per_page={SUPERSEDE_PAGE}&page=1")
    try:
        body = fetch(url)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    batch = body.get("workflow_runs") if isinstance(body, dict) else None
    if not isinstance(batch, list) or len(batch) > SUPERSEDE_PAGE:
        return False
    return any(isinstance(r, dict) and fa._int_in(r.get("id"), 1, fa.MAX_INT) and r["id"] > run_id
               and r.get("head_branch") == branch and r.get("workflow_id") == workflow for r in batch)


def _when(job: dict[str, Any]) -> tuple[str, str, str] | None:
    """(name, started_at, completed_at): what a carried-over copy keeps of its original. None if malformed."""
    name, start, end = job.get("name"), job.get("started_at"), job.get("completed_at")
    if isinstance(name, str) and isinstance(start, str) and isinstance(end, str) \
            and _STAMP.fullmatch(start) and _STAMP.fullmatch(end):
        return name, start, end
    return None


def _carried_over(fetch: FetchJson, api: str, run: dict[str, Any], jobs: list[dict[str, Any]],
                  job_ids: dict[int, tuple[int, int]]) -> dict[int, tuple[int, int]]:
    """Point each slice job that a "Re-run failed jobs" attempt copied (it started before this attempt did) at
    the earliest attempt holding a job with its name and timestamps: that job ran and uploaded the artifact.
    A job that really reran in this attempt (or has no readable timestamps) keeps this attempt's artifact."""
    started = run.get("run_started_at")
    if not (isinstance(started, str) and _STAMP.fullmatch(started)):
        return job_ids
    by_id = {job.get("id"): job for job in jobs}
    copies = {s: when for s, (jid, _) in job_ids.items()
              if (when := _when(by_id.get(jid) or {})) is not None and when[1] < started}
    out = dict(job_ids)
    for earlier in range(1, run["run_attempt"]):
        if not copies:
            break
        listed = _pages(fetch, f"{api}/actions/runs/{run['id']}/attempts/{earlier}/jobs", "jobs", "jobs_unavailable")
        originals = {when: job["id"] for job in listed
                     if (when := _when(job)) is not None and fa._int_in(job.get("id"), 1, fa.MAX_INT)
                     and job.get("run_attempt", earlier) == earlier}
        for slice_no, when in list(copies.items()):
            if when in originals:
                out[slice_no] = (originals[when], earlier)
                del copies[slice_no]
    return out


def _artifact_id(artifacts: list[dict[str, Any]], name: str) -> int:
    """The one unexpired artifact called ``name``; missing/expired/ambiguous are fixed codes."""
    named = [a for a in artifacts if a.get("name") == name]
    live = [a for a in named if a.get("expired") is False]
    if not live:
        raise Unavailable("expired" if named else "missing")
    if len(live) != 1 or not fa._int_in(live[0].get("id"), 1, fa.MAX_INT):
        raise Unavailable("invalid")
    return live[0]["id"]


def _entries(meta: dict[str, Any]) -> list[dict[str, Any]]:
    provenance = {k: meta[k] for k in ("run_attempt", "source_sha", "slice", "slices")}
    return [{"test_id": e["test_id"], "retried": e["retried"], "first_attempt_excerpt": "", **provenance}
            for e in meta["flaky"]]


def assess_run(repo: str, run: dict[str, Any], fetch: FetchJson, token: str) -> RunResult:
    """Validated entries for every slice of this run attempt, or ``Unavailable``."""
    run_id, attempt, head_sha = run.get("id"), run.get("run_attempt", 1), run.get("head_sha")
    if not (isinstance(repo, str) and fa._REPO.fullmatch(repo)
            and fa._int_in(run_id, 1, fa.MAX_INT) and fa._int_in(attempt, 1, fa.MAX_INT)
            and isinstance(head_sha, str) and fa._SHA.fullmatch(head_sha)):
        raise Unavailable("invalid")
    api = f"https://api.github.com/repos/{repo}"
    jobs = _pages(fetch, f"{api}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs", "jobs_unavailable")
    try:
        slices, job_ids = expected_jobs(jobs, attempt, cancelled=run.get("conclusion") == "cancelled")
    except Unavailable:
        if run.get("conclusion") == "cancelled" and superseded(fetch, api, run):
            return RunResult("not_applicable")
        raise
    if not job_ids:
        return RunResult("not_applicable")
    if attempt > 1:
        job_ids = _carried_over(fetch, api, run, jobs, job_ids)
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
