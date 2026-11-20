"""GitHub Actions run / PR checks -> watch poll. [daveey]

Wraps :func:`hermes_cli.kanban_ci_progress.ci_run_progress` and :func:`pr_progress` (keeping their ``fetch=``
injection) and adds what a card needs to show light blue or red: the JOB's state (queued / running / succeeded /
failed / cancelled) separately from the SERVICE's health (ok / degraded / down / unreachable / auth_failed /
rate_limited). A GitHub error is no longer logged and forgotten: it becomes a health record.

Cost: a run poll is one jobs read (shared with ``ci_run_progress``) plus one run read, only when every listed job is
complete (to confirm the run's own conclusion: later jobs may not exist yet). A PR poll reads the PR and its head's
runs again on top of ``pr_progress``'s own reads: three or four calls per PR poll.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

from hermes_cli import kanban_ci_progress as kcp

SERVICE_NAME = "GitHub Actions"
DEFAULT_RETRY_AFTER = 300
DETAIL_MAX = 300
_RETRY_AFTER = re.compile(r"retry[- _]after\D{0,3}(\d+)", re.IGNORECASE)
_RATE = re.compile(r"http 429|rate limit|secondary rate|abuse detection", re.IGNORECASE)
_AUTH = re.compile(r"http 401|bad credentials|authentication required|gh auth login", re.IGNORECASE)
_UNREACHABLE = re.compile(r"timed out|timeout|no such host|could not resolve|temporary failure|connection (?:refused|reset)"
                          r"|network is unreachable|dial tcp|\beof\b", re.IGNORECASE)
_FAILED = ("failure", "timed_out", "startup_failure", "action_required")
_PENDING = ("queued", "waiting", "requested", "pending")


@dataclass
class Watched:
    """One adapter poll: ``poll`` is None when the sweep budget cut the read off (nothing to record yet)."""

    poll: Optional[dict]
    progress: kcp.Progress
    adapter: str
    scope: str
    service_name: str = SERVICE_NAME


def adapter_for(ref: str, hint: str = "") -> Optional[str]:
    if kcp.run_target(ref, hint) is not None:
        return "github_run"
    if kcp.pr_target(ref, hint) is not None:
        return "github_pr_checks"
    return None


def classify(detail: str, *, no_credential: bool = False, gh_missing: bool = False) -> tuple[str, Optional[int]]:
    """``(health, retry_after)`` for a GitHub read that failed with ``detail``."""
    text = detail or ""
    if no_credential or _AUTH.search(text):
        return "auth_failed", None
    if gh_missing:
        return "probe_broken", None
    if _RATE.search(text):
        m = _RETRY_AFTER.search(text)
        return "rate_limited", int(m.group(1)) if m else DEFAULT_RETRY_AFTER
    if _UNREACHABLE.search(text):
        return "unreachable", None
    return "down", None


def _failed(p: kcp.Progress, adapter: str, scope: str, detail: Optional[str] = None) -> Watched:
    health, retry = classify(detail if detail is not None else p.detail, no_credential=p.no_credential,
                             gh_missing=p.gh_missing)
    text = (detail if detail is not None else p.detail) or health
    return Watched({"job": "unknown", "service": health, "detail": text[-DETAIL_MAX:], "url": None,
                    "retry_after": retry}, p, adapter, scope)


def _conclusion_job(conclusion: Optional[str]) -> str:
    if conclusion in _FAILED:
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion in ("success", "neutral", "skipped"):
        return "succeeded"
    return "unknown"


def _run_job(jobs: list, repo: str, run_id: str, fetch_run: Optional[Callable]) -> tuple[str, str]:
    """``(job state, detail)`` of a run from its listed jobs; the run's own read confirms a finished one."""
    running = [j for j in jobs if isinstance(j, dict) and j.get("status") == "in_progress"]
    if running:
        return "running", f"job '{running[0].get('name') or 'job'}' running"
    unfinished = [j for j in jobs if isinstance(j, dict) and j.get("status") != "completed"]
    if not jobs:
        return "queued", "no jobs created yet"
    if unfinished:
        started = len(jobs) > len(unfinished)
        return ("running", "waiting on later jobs") if started else ("queued", "queued, no runner yet")
    run = (fetch_run or (lambda r, i: kcp._gh_api(f"repos/{r}/actions/runs/{i}")))(repo, run_id) or {}
    if run.get("status") != "completed":
        return "running", "finishing up"
    job = _conclusion_job(run.get("conclusion"))
    return job, f"run {run.get('conclusion') or 'finished'}"


def poll(ref: str, hint: str = "", *, parked_at: Optional[float] = None, now: Optional[float] = None,
         fetch: Optional[kcp.Fetch] = None, fetch_runners: Optional[kcp.FetchRunners] = None,
         fetch_run: Optional[Callable[[str, str], dict]] = None,
         deadline: Optional[float] = None) -> Optional[Watched]:
    """Poll the GitHub run or PR ``ref`` names; None when it names neither. Never raises."""
    adapter = adapter_for(ref, hint)
    if adapter is None:
        return None
    try:
        if adapter == "github_run":
            return _poll_run(ref, hint, now, fetch, fetch_runners, fetch_run, deadline)
        return _poll_pr(ref, hint, parked_at, now, fetch, fetch_runners, deadline)
    except kcp.GhMissing as exc:
        return _failed(kcp.Progress("unknown", gh_missing=True, detail=str(exc)), adapter, "")
    except kcp.NoCredential as exc:
        return _failed(kcp.Progress("unknown", no_credential=True, detail=str(exc)), adapter, "")
    except (OSError, RuntimeError, ValueError, AttributeError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        return _failed(kcp.Progress("unknown", detail=str(exc)), adapter, "")


def _poll_run(ref, hint, now, fetch, fetch_runners, fetch_run, deadline) -> Watched:
    seen: dict = {}

    def jobs_fetch(repo: str, run_id: str) -> dict:
        data = (fetch or kcp._fetch_jobs)(repo, run_id)
        seen["jobs"] = data
        return data

    p = kcp.ci_run_progress(ref, hint=hint, fetch=jobs_fetch, fetch_runners=fetch_runners, now=now,
                            deadline=deadline)
    scope = p.repo
    if p.state == "unknown":
        return Watched(None, p, "github_run", scope) if p.deferred else _failed(p, "github_run", scope)
    url = f"https://github.com/{p.repo}/actions/runs/{p.run_id}"
    if p.state in (kcp.NOT_STARTING, kcp.BUSY):
        detail = kcp.describe(p) if p.state == kcp.NOT_STARTING else p.detail
        return Watched({"job": "queued", "service": "degraded", "detail": detail[:DETAIL_MAX], "url": url,
                        "retry_after": None}, p, "github_run", scope)
    try:
        job, detail = _run_job((seen.get("jobs") or {}).get("jobs") or [], p.repo, p.run_id, fetch_run)
    except kcp.NoCredential as exc:
        return _failed(kcp.Progress("unknown", p.repo, p.run_id, detail=str(exc), no_credential=True),
                       "github_run", scope)
    except kcp.GhMissing as exc:
        return _failed(kcp.Progress("unknown", p.repo, p.run_id, detail=str(exc), gh_missing=True),
                       "github_run", scope)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _failed(kcp.Progress("unknown", p.repo, p.run_id, detail=str(exc)), "github_run", scope)
    return Watched({"job": job, "service": "ok", "detail": detail, "url": url, "retry_after": None}, p,
                   "github_run", scope)


def _poll_pr(ref, hint, parked_at, now, fetch, fetch_runners, deadline) -> Watched:
    repo, number, sha = kcp.pr_target(ref, hint)  # type: ignore[misc]  # adapter_for proved it is a PR ref
    url = f"https://github.com/{repo}/pull/{number}"
    p = kcp.pr_progress(repo, number, sha, parked_at, fetch=fetch, fetch_runners=fetch_runners, now=now,
                        deadline=deadline)
    if p.state == "unknown" and not p.settling:
        return Watched(None, p, "github_pr_checks", repo) if p.deferred else _failed(p, "github_pr_checks", repo)
    if p.state == kcp.NOT_STARTING:
        detail = p.detail if p.per_pr else kcp.describe(p)
        return Watched({"job": "queued", "service": "degraded", "detail": detail[:DETAIL_MAX], "url": url,
                        "retry_after": None}, p, "github_pr_checks", repo)
    api = kcp._gh_api
    try:
        pr = api(f"repos/{repo}/pulls/{number}") or {}
        if pr.get("state") != "open" or pr.get("merged"):
            # The existing sweep contract (a finished or moved PR never wakes the park) is kept: the job is unknown.
            return Watched({"job": "unknown", "service": "ok", "detail": "PR merged" if pr.get("merged") else "PR closed",
                            "url": url, "retry_after": None}, p, "github_pr_checks", repo)
        head = str((pr.get("head") or {}).get("sha") or "").lower()
        if sha and not head.startswith(sha.lower()):
            return Watched({"job": "unknown", "service": "ok", "detail": f"head moved off {sha[:10]}", "url": url,
                            "retry_after": None}, p, "github_pr_checks", repo)
        runs = [r for r in (api(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100") or {}).get(
            "workflow_runs") or [] if isinstance(r, dict)]
    except kcp.NoCredential as exc:
        return _failed(kcp.Progress("unknown", repo, no_credential=True, detail=str(exc)), "github_pr_checks", repo)
    except kcp.GhMissing as exc:
        return _failed(kcp.Progress("unknown", repo, gh_missing=True, detail=str(exc)), "github_pr_checks", repo)
    except (OSError, RuntimeError, ValueError, AttributeError, subprocess.TimeoutExpired) as exc:
        return _failed(kcp.Progress("unknown", repo, detail=str(exc)), "github_pr_checks", repo)
    if not runs or any(r.get("status") in _PENDING for r in runs) and not any(
            r.get("status") == "in_progress" for r in runs):
        job, detail = "queued", "checks queued" if runs else "no checks started yet"
    elif any(r.get("status") != "completed" for r in runs):
        job, detail = "running", "checks running"
    else:
        concl = [r.get("conclusion") for r in runs]
        job = ("failed" if any(c in _FAILED for c in concl) else "cancelled" if "cancelled" in concl
               else "succeeded")
        detail = f"checks {job}"
    return Watched({"job": job, "service": "ok", "detail": detail, "url": url, "retry_after": None}, p,
                   "github_pr_checks", repo)
