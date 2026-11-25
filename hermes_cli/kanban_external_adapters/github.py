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

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional

from hermes_cli import kanban_ci_progress as kcp

logger = logging.getLogger(__name__)

SERVICE_NAME = "GitHub Actions"
DEFAULT_RETRY_AFTER = 300
DETAIL_MAX = 300
_RETRY_AFTER = re.compile(r"retry[- _]after\D{0,3}(\d+)", re.IGNORECASE)
REQUIRED_TTL_SECONDS = 600  # a branch's required checks change rarely: one read per base per 10 min
SETTLE_SECONDS = 60  # with no required checks: how long after the last run finished before "succeeded" is final
_REQUIRED: dict[tuple[str, str], tuple[float, set[str]]] = {}
_STATUS_CONCLUSION = {"success": "success", "failure": "failure", "error": "failure"}
# The local quota guard / shared brake refusing our own call is our pacing, not GitHub's health: never red.
_RATE = re.compile(r"http 429|rate limit|secondary rate|abuse detection|quota guard|quota brake", re.IGNORECASE)
_AUTH = re.compile(r"http 40[13]|bad credentials|authentication required|gh auth login|resource not accessible"
                   r"|saml", re.IGNORECASE)
_HTTP_AUTH = re.compile(r"http 40[13]|bad credentials|resource not accessible|saml", re.IGNORECASE)
_REF = re.compile(r"http (?:404|410|422)\b", re.IGNORECASE)  # this run/PR, not the service: deleted, gone, invalid
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
    head_sha: str = ""  # a run poll: the commit the run built (from its run or job read), "" when unread


def adapter_for(ref: str, hint: str = "") -> Optional[str]:
    if kcp.run_target(ref, hint) is not None:
        return "github_run"
    if kcp.pr_target(ref, hint) is not None:
        return "github_pr_checks"
    return None


def classify(detail: str, *, no_credential: bool = False, gh_missing: bool = False) -> tuple[str, Optional[int]]:
    """``(health, retry_after)`` for a GitHub read that failed with ``detail``."""
    text = detail or ""
    if _RATE.search(text):  # before auth: GitHub's rate limits answer 403 too
        m = _RETRY_AFTER.search(text)
        return "rate_limited", int(m.group(1)) if m else DEFAULT_RETRY_AFTER
    if no_credential or _AUTH.search(text):
        return "auth_failed", None
    if gh_missing or _REF.search(text):
        return "probe_broken", None
    if _UNREACHABLE.search(text):
        return "unreachable", None
    return "down", None


def _failed(p: kcp.Progress, adapter: str, scope: str, detail: Optional[str] = None) -> Watched:
    health, retry = classify(detail if detail is not None else p.detail, no_credential=p.no_credential,
                             gh_missing=p.gh_missing)
    text = (detail if detail is not None else p.detail) or health
    # No usable ``gh`` login on THIS host (exit 4, an unusable broker prefix) is the host's trouble, not the
    # service's: the card is red, the operator is warned hourly (``_warn_disabled``), and no card is woken for it.
    host_only = health == "auth_failed" and not _HTTP_AUTH.search(text)
    return Watched({"job": "unknown", "service": health, "detail": text[-DETAIL_MAX:], "url": None,
                    "retry_after": retry, "host_only": host_only}, p, adapter, scope)


def _conclusion_job(conclusion: Optional[str]) -> str:
    if conclusion in _FAILED:
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion in ("success", "neutral", "skipped"):
        return "succeeded"
    return "unknown"


def _run_job(jobs: list, repo: str, run_id: str, fetch_run: Optional[Callable],
             seen: Optional[dict] = None) -> tuple[str, str]:
    """``(job state, detail)`` of a run from its listed jobs; the run's own read decides a finished or empty one
    (and is kept in ``seen["run"]``)."""
    running = [j for j in jobs if isinstance(j, dict) and j.get("status") == "in_progress"]
    if running:
        return "running", f"job '{running[0].get('name') or 'job'}' running"
    unfinished = [j for j in jobs if isinstance(j, dict) and j.get("status") != "completed"]
    if unfinished:
        started = len(jobs) > len(unfinished)
        return ("running", "waiting on later jobs") if started else ("queued", "queued, no runner yet")
    run = (fetch_run or (lambda r, i: kcp._gh_api(f"repos/{r}/actions/runs/{i}")))(repo, run_id) or {}
    if seen is not None:
        seen["run"] = run
    if run.get("status") != "completed":
        # A run cancelled before any job was created lists none and is still finished: only its own read tells.
        return ("running", "finishing up") if jobs else ("queued", "no jobs created yet")
    job = _conclusion_job(run.get("conclusion"))
    return job, f"run {run.get('conclusion') or 'finished'}"


def poll(ref: str, hint: str = "", *, parked_at: Optional[float] = None, now: Optional[float] = None,
         fetch: Optional[kcp.Fetch] = None, fetch_runners: Optional[kcp.FetchRunners] = None,
         fetch_run: Optional[Callable[[str, str], dict]] = None,
         deadline: Optional[float] = None, api: Optional[Callable[[str], dict]] = None) -> Optional[Watched]:
    """Poll the GitHub run or PR ``ref`` names; None when it names neither. Never raises. ``api`` replaces
    ``gh api <path>`` for the PR adapter's own reads (the verdict); ``pr_progress`` keeps its own."""
    adapter = adapter_for(ref, hint)
    if adapter is None:
        return None
    try:
        if adapter == "github_run":
            return _poll_run(ref, hint, now, fetch, fetch_runners, fetch_run, deadline)
        return _poll_pr(ref, hint, parked_at, now, fetch, fetch_runners, deadline, api)
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
        jobs = (seen.get("jobs") or {}).get("jobs") or []
        job, detail = _run_job(jobs, p.repo, p.run_id, fetch_run, seen)
    except kcp.NoCredential as exc:
        return _failed(kcp.Progress("unknown", p.repo, p.run_id, detail=str(exc), no_credential=True),
                       "github_run", scope)
    except kcp.GhMissing as exc:
        return _failed(kcp.Progress("unknown", p.repo, p.run_id, detail=str(exc), gh_missing=True),
                       "github_run", scope)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _failed(kcp.Progress("unknown", p.repo, p.run_id, detail=str(exc)), "github_run", scope)
    return Watched({"job": job, "service": "ok", "detail": detail, "url": url, "retry_after": None}, p,
                   "github_run", scope, head_sha=_head_sha(seen.get("run"), jobs))


def _head_sha(run: Optional[dict], jobs: list) -> str:
    """The commit a run built: its own read's ``head_sha``, else its jobs' one (all jobs of a run share it)."""
    for item in [run or {}, *[j for j in jobs if isinstance(j, dict)]]:
        sha = str((item or {}).get("head_sha") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    return ""


def poll_commit(repo: str, sha: str, from_run: int, *, fetch: Optional[kcp.Fetch] = None,
                fetch_runners: Optional[kcp.FetchRunners] = None, now: Optional[float] = None,
                deadline: Optional[float] = None, api: Optional[Callable[[str], dict]] = None) -> Watched:
    """Every workflow run of commit ``sha`` as ONE poll, for a card parked on run ``from_run`` of it that
    succeeded while others may still run (the whole-commit wait). One ``actions/runs?head_sha=`` read; a
    superseded attempt (an older run of a workflow that ran again on the same event) never decides.

    ``job``: ``failed`` / ``cancelled`` as soon as any run ended so (a failure is a result: the card wakes at
    once), else ``running`` while any run is not completed, else ``succeeded``. ``pending_runs`` and ``runs``
    (id, name, status, conclusion) ride on the poll. A queued run goes through :func:`kcp.ci_run_progress` (at
    most ``kcp.PR_QUEUED_RUNS_MAX``, as a PR's head): a run no runner takes is ``not_starting`` as on any CI park.
    Raises on a failed read (:class:`kcp.OverBudget` included): the caller falls back to the single run."""
    if deadline is not None and time.monotonic() >= deadline:
        raise kcp.OverBudget("sweep budget spent before the commit's runs were read")
    data = (api or kcp._gh_api)(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100") or {}
    listed = data.get("workflow_runs")
    if not isinstance(listed, list):
        raise ValueError(f"no workflow_runs in the runs of {sha[:10]}")
    total = (data or {}).get("total_count")
    if isinstance(total, int) and total > len(listed):
        raise ValueError(f"commit {sha[:10]} has {total} workflow runs, {len(listed)} read: not judged on a page")
    runs = sorted(_deciding_runs([r for r in listed if isinstance(r, dict) and r.get("id")]),
                  key=lambda r: int(r["id"]))
    if not any(int(r["id"]) == int(from_run) for r in runs):
        # The watched run itself is missing from the list (paged out, or a newer attempt of its workflow): it is
        # known finished and green, so it still counts in the verdict and the comment.
        runs.append({"id": int(from_run), "name": f"run {from_run}", "status": "completed", "conclusion": "success"})
    summary = [{"id": int(r["id"]), "name": str(r.get("name") or "run"), "status": str(r.get("status") or ""),
                "conclusion": r.get("conclusion"), "updated_at": r.get("updated_at")} for r in runs]
    pending = [r["id"] for r in summary if r["status"] != "completed"]
    done = [_conclusion_job(r["conclusion"]) for r in summary if r["status"] == "completed"]
    if "failed" in done:
        job = "failed"
    elif "cancelled" in done:
        job = "cancelled"
    else:
        job = "running" if pending else "succeeded"
    p = kcp.Progress("ok", repo, str(from_run))
    if job == "running":
        for run in [r for r in summary if r["status"] in _PENDING][:kcp.PR_QUEUED_RUNS_MAX]:
            got = kcp.ci_run_progress(f"{repo}/actions/runs/{run['id']}", fetch=fetch, fetch_runners=fetch_runners,
                                      now=now, deadline=deadline)
            if got.state == kcp.NOT_STARTING:
                p = got
                break
    left = f", {len(pending)} still running" if pending else ""
    detail = f"commit {sha[:10]}: {len(summary)} workflow run(s){left}"
    return Watched({"job": job, "service": "ok", "detail": detail, "url": f"https://github.com/{repo}/commit/{sha}",
                    "retry_after": None, "runs": summary, "pending_runs": pending, "head_sha": sha,
                    "from_run": int(from_run)}, p, "github_run", repo, head_sha=sha)


def _poll_pr(ref, hint, parked_at, now, fetch, fetch_runners, deadline, api=None) -> Watched:
    repo, number, sha = kcp.pr_target(ref, hint)  # type: ignore[misc]  # adapter_for proved it is a PR ref
    url = f"https://github.com/{repo}/pull/{number}"
    p = kcp.pr_progress(repo, number, sha, parked_at, fetch=fetch, fetch_runners=fetch_runners, now=now,
                        deadline=deadline)
    if p.deferred:
        return Watched(None, p, "github_pr_checks", repo)
    if p.state == "unknown" and not p.settling:
        return Watched(None, p, "github_pr_checks", repo) if p.deferred else _failed(p, "github_pr_checks", repo)
    if p.state == kcp.NOT_STARTING:
        detail = p.detail if p.per_pr else kcp.describe(p)
        return Watched({"job": "queued", "service": "degraded", "detail": detail[:DETAIL_MAX], "url": url,
                        "retry_after": None}, p, "github_pr_checks", repo)
    api = api or kcp._gh_api
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
        job, detail = _pr_verdict(api, repo, head, str((pr.get("base") or {}).get("ref") or ""), now, deadline)
    except kcp.OverBudget as exc:
        return Watched(None, kcp.Progress("unknown", repo, pr=number, detail=str(exc), deferred=True),
                       "github_pr_checks", repo)
    except kcp.NoCredential as exc:
        return _failed(kcp.Progress("unknown", repo, no_credential=True, detail=str(exc)), "github_pr_checks", repo)
    except kcp.GhMissing as exc:
        return _failed(kcp.Progress("unknown", repo, gh_missing=True, detail=str(exc)), "github_pr_checks", repo)
    except (OSError, RuntimeError, ValueError, AttributeError, subprocess.TimeoutExpired) as exc:
        return _failed(kcp.Progress("unknown", repo, detail=str(exc)), "github_pr_checks", repo)
    return Watched({"job": job, "service": "ok", "detail": detail, "url": url, "retry_after": None}, p,
                   "github_pr_checks", repo)


def _budget(deadline: Optional[float]) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise kcp.OverBudget("sweep budget spent before the PR's checks were read")


def _workflow(run: dict) -> object:
    return run.get("workflow_id") or run.get("name")


def _run_key(run: dict) -> tuple:
    """One workflow on one trigger (a re-trigger on the same event supersedes the attempt before it)."""
    return _workflow(run), run.get("event")


def _deciding_runs(listed: list) -> list[dict]:
    """The runs that decide a commit's verdict: the newest run of each workflow, plus an older run of the same
    workflow on ANOTHER event only when it FAILED (a push run failing beside a green pull_request run is a result).
    An older sibling that was cancelled (a concurrency group shared by both events), skipped or is still pending
    stays superseded, as before."""
    newest = _newest(listed, _workflow)
    ids = {r.get("id") for r in newest}
    return newest + [r for r in _newest(listed, _run_key) if r.get("id") not in ids
                     and r.get("status") == "completed" and r.get("conclusion") in _FAILED]


def _newest(items: list, key: Callable[[dict], object]) -> list[dict]:
    """The newest (highest id) item per ``key``: a rerun or a superseding run replaces the attempt before it."""
    best: dict = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        k = key(item)
        if k not in best or (item.get("id") or 0) > (best[k].get("id") or 0):
            best[k] = item
    return list(best.values())


def required_checks(api: Callable[[str], dict], repo: str, base: str, deadline: Optional[float] = None) -> set[str]:
    """Names of the checks ``base`` requires (classic protection and rulesets), cached ``REQUIRED_TTL_SECONDS``.
    A branch that requires none answers with none; a failed read raises (the poll is a health record)."""
    if not base:
        return set()
    seen = _REQUIRED.get((repo, base))
    if seen is not None and time.monotonic() - seen[0] < REQUIRED_TTL_SECONDS:
        return seen[1]
    _budget(deadline)
    branch = api(f"repos/{repo}/branches/{base}") or {}
    checks = (branch.get("protection") or {}).get("required_status_checks") or {}
    names = {str(c) for c in checks.get("contexts") or [] if c}
    _budget(deadline)
    rules = api(f"repos/{repo}/rules/branches/{base}") or []
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
                if isinstance(check, dict) and check.get("context"):
                    names.add(str(check["context"]))
    _REQUIRED[(repo, base)] = (time.monotonic(), names)
    return names


def _pr_verdict(api: Callable[[str], dict], repo: str, head: str, base: str, now: Optional[float],
                deadline: Optional[float]) -> tuple[str, str]:
    """``(job, detail)`` of a PR head's checks. Terminal only on evidence that nothing more is coming:

    * the base requires checks: every required check exists on the head and its newest attempt completed. One
      workflow finishing first is not "succeeded"; an optional workflow still running does not hold the verdict.
    * no required checks: the newest run of every workflow completed, and for a success the newest of them
      finished ``SETTLE_SECONDS`` ago (a later workflow may not have been created yet).

    A superseded attempt (an older cancelled run of a workflow that ran again) never decides."""
    _budget(deadline)
    data = api(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100") or {}
    listed = data.get("workflow_runs") or []
    total = data.get("total_count")
    if isinstance(total, int) and total > len(listed):
        # A verdict on one page could miss a failed or running workflow: not judged, the park's deadline stands.
        logger.warning("kanban PR checks: head %s of %s has %d workflow runs, %d read: not judged on one page",
                       head[:10], repo, total, len(listed))
        return "unknown", f"{total} workflow runs on head {head[:10]}, {len(listed)} read: not judged on one page"
    runs = _deciding_runs(listed)
    started = any(r.get("status") in ("in_progress", "completed") for r in runs)
    required = required_checks(api, repo, base, deadline)
    if required:
        _budget(deadline)
        checks = (api(f"repos/{repo}/commits/{head}/check-runs?per_page=100") or {}).get("check_runs") or []
        state: dict[str, Optional[str]] = {}
        for check in _newest(checks, lambda c: c.get("name")):
            if check.get("name") in required:
                done = check.get("status") == "completed"
                state[str(check["name"])] = (check.get("conclusion") or "unknown") if done else None
        if required - set(state):  # a required context may be a commit status, not a check run
            _budget(deadline)
            for st in (api(f"repos/{repo}/commits/{head}/status") or {}).get("statuses") or []:
                if isinstance(st, dict) and st.get("context") in required and st["context"] not in state:
                    state[str(st["context"])] = _STATUS_CONCLUSION.get(str(st.get("state")))
        waiting = sorted(name for name in required if state.get(name) is None)
        if waiting:
            more = f" (+{len(waiting) - 1} more)" if waiting[1:] else ""
            return "running" if started else "queued", f"waiting on required check '{waiting[0]}'{more}"
        concl = list(state.values())
    else:
        if not runs or (any(r.get("status") in _PENDING for r in runs) and not started):
            return "queued", "checks queued" if runs else "no checks started yet"
        if any(r.get("status") != "completed" for r in runs):
            return "running", "checks running"
        concl = [r.get("conclusion") for r in runs]
    if any(c in (*_FAILED, "stale") for c in concl):
        return "failed", "checks failed"
    if "cancelled" in concl:
        return "cancelled", "checks cancelled"
    if any(c not in ("success", "neutral", "skipped") for c in concl):
        return "unknown", "checks completed without a known conclusion"
    if not required:
        newest = max((kcp._epoch(r.get("updated_at")) or 0) for r in runs)
        if (time.time() if now is None else float(now)) - newest < SETTLE_SECONDS:
            return "running", "checks settling (a later workflow may still start)"
    return "succeeded", "checks succeeded"
