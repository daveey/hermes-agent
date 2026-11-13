#!/usr/bin/env python3
"""Batched CI review summaries for open same-repo PRs.

Replaces the per-run live poller. One scheduled (hourly) invocation lists every
open PR whose head lives in this repository (forks are excluded), picks the
newest ``CI`` ``pull_request`` run on each PR's exact head SHA, builds a
snapshot with the :mod:`live_comment` helpers, and upserts the review comment.

Targeted mode (``--head-sha`` / ``$TARGET_HEAD_SHA``): the workflow's
``workflow_run`` trigger passes the head of the CI run that just completed, and
only the open PR(s) at exactly that head are refreshed. Why: ``hermes-core-wt
ship`` lands only once this comment names the PR's exact head ("ran on
<sha>"); every ship rebase changes the head, so an approved, CI-green PR
otherwise waited up to an hour for the next sweep (PR1104, 2026-11-12).

Safety / behaviour:

  - Only API reads and comment writes. No PR code is checked out or run.
  - No sleep/poll loop: one pass, then exit.
  - Immediately before writing, the PR is re-read. If it was closed or its
    head moved, the PR is skipped (the snapshot would be stale).
  - Each PR is isolated: an error on one PR is recorded and the rest still
    run. Any error (listing, collection, write) makes the exit code 1.
  - The comment keeps the ``<!-- hermes-ci-review-bot -->`` marker, so it
    replaces earlier comments from the live poller. The footer names both
    refresh triggers (a CI or Docker completion on the head, and the hourly
    sweep), so the snapshot is not mistaken for a live view.
  - Ordering: the hourly sweep and a targeted run can both hold a snapshot of
    the same PR. Each write records the head and the writing run's start in a
    hidden state line (``<!-- hermes-ci-review-bot:state ... -->``). A run
    writes only if no snapshot of that same head from a run that started
    later is already there; otherwise it skips and logs why, so an older run
    cannot overwrite a newer run's result until the next sweep.
  - Targeted runs triggered by a watched sibling (Docker) completing while CI
    on that head is still running write nothing: CI's own completion
    refreshes the PR with both results.
  - A failed review-status artifact download is retried with bounded backoff
    (90 s of sleeps on a targeted refresh, 15 s per PR on the sweep), and
    every run is bounded by wall clock (RUN_BUDGET_S from its start, counting
    each attempt's API and blob timeouts, not only the sleeps): retries stop,
    and the sweep stops starting PRs, while the job can still finish inside
    its timeout.
    When a targeted refresh still cannot build the snapshot, it marks the
    comment "refresh failed on <head> at <time>" (state line ``failed=``),
    so the ship probe and readers do not take a stale "running on" for a
    live one, and a CI- or Docker-triggered refresh queues one delayed
    re-refresh of that head (a ``workflow_dispatch`` with ``head_sha``) unless
    one is already queued for it (state line ``requeued=``) or the comment
    keeps that head's completed ("ran on") snapshot. The re-refresh itself
    never queues another; after it, the hourly sweep is next. Why: PR1661's
    CI-completion refresh (run 43279407344, 2026-11-12 18:24Z) failed one
    download, went red, and nothing retried; the approved, green PR waited
    45 min on a "running on" comment.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

CI_WORKFLOW_NAME = "CI"
DEFAULT_WATCH_WORKFLOWS = ["Docker Build, Test, and Publish"]
WORKFLOW_FILE = "ci-review-comment.yml"

# Retry delays (seconds) for a failed review-status artifact download, one budget per PR snapshot. A targeted refresh
# is the only writer that runs right after CI completes, so it waits longer (~90 s); the sweep visits every open PR.
TARGETED_DOWNLOAD_RETRY_DELAYS = (5, 10, 15, 25, 35)
BATCH_DOWNLOAD_RETRY_DELAYS = (5, 10)
# The one delayed re-refresh a failed CI-triggered refresh queues, and the cap on any requested delay (job timeout 15m).
RE_REFRESH_DELAY_S = 300
MAX_REFRESH_DELAY_S = 600
# Wall-clock budget of one run, from the start of main() (a re-refresh's delay included): the job's timeout-minutes
# less headroom for checkout/setup and the last in-flight write. Download retries stop, and the sweep stops starting
# PRs, once a full attempt (lc.DOWNLOAD_ATTEMPT_MAX_S: its API and blob timeouts) would not finish inside it, so a
# delayed re-refresh or a sweep during an artifact outage ends red with a log line instead of being cancelled.
JOB_TIMEOUT_S = 15 * 60
RUN_HEADROOM_S = 180
RUN_BUDGET_S = JOB_TIMEOUT_S - RUN_HEADROOM_S
# What a PR needs beyond one download attempt (its run/job reads, the head re-read and the comment write).
PR_MIN_BUDGET_S = 30
_sleep = time.sleep
_clock = time.monotonic


class SnapshotError(RuntimeError):
    """The snapshot for a PR could not be built (collection or review-status download failed); nothing was written."""

_HERE = Path(__file__).resolve().parent


def _load_live_comment():
    spec = importlib.util.spec_from_file_location("live_comment", _HERE / "live_comment.py")
    if spec is None or spec.loader is None:
        raise ImportError("Failed to load live_comment.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("live_comment", mod)
    spec.loader.exec_module(mod)
    return mod


lc = _load_live_comment()


def eligible_prs(prs: list[dict], repo: str) -> list[dict]:
    """Open PRs whose head repository is ``repo`` (forks/deleted forks out)."""
    out = []
    for pr in prs:
        if pr.get("state", "open") != "open":
            continue
        head = pr.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name", "")
        if head_repo.lower() != repo.lower() or not head.get("sha"):
            continue
        out.append(pr)
    return out


def select_ci_run(runs: list[dict], head_sha: str) -> dict | None:
    """Newest CI ``pull_request`` run whose head SHA is exactly ``head_sha``."""
    best: dict | None = None
    for r in runs:
        if r.get("name") != CI_WORKFLOW_NAME or r.get("event") != "pull_request":
            continue
        if r.get("head_sha") != head_sha:
            continue
        key = (str(r.get("created_at", "")), int(r.get("id", 0) or 0))
        if best is None or key > (str(best.get("created_at", "")), int(best.get("id", 0) or 0)):
            best = r
    return best


def snapshot_commit_info(pr: dict, run: dict, repo: str, now: datetime, *, completed: bool = False) -> str:
    sha = run["head_sha"]
    url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/pull/{pr['number']}/commits/{sha}"
    msg = ((run.get("head_commit") or {}).get("message") or "").split("\n")[0][:60]
    tail = f" — {msg}" if msg else ""
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    phase = "ran on" if completed else "running on"
    return (
        f"<sub>{phase} [{sha[:7]}]({url}){tail} · snapshot of CI taken {stamp}; "
        f"{REFRESH_NOTE}</sub>"
    )


# Both triggers in ci-review-comment.yml: workflow_run (CI or a watched sibling completed on this head) and cron.
REFRESH_NOTE = "refreshed when CI or Docker completes on this head, and hourly; not live"

STATE_PREFIX = "<!-- hermes-ci-review-bot:state "
BOT_MARKER = "<!-- hermes-ci-review-bot -->"
_STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width, so stamps order as strings
_STATE_RE = re.compile(
    r"<!-- hermes-ci-review-bot:state head=([0-9a-f]{40}) "
    r"started=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z) run=(\S*?)"
    r"(?: failed=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z))?"
    r"(?: requeued=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z))? -->"
)
# The snapshot footer (snapshot_commit_info) or a failed-refresh footer (failed_footer): one line each.
_FOOTER_RE = re.compile(r"<sub>(?:running on|ran on|refresh failed on) [^\n]*</sub>")


def _stamp(started: datetime) -> str:
    return started.astimezone(UTC).strftime(_STAMP)


def state_line(head_sha: str, started: datetime, run_id: str = "", failed: datetime | None = None,
               requeued: datetime | None = None) -> str:
    """The hidden line recording which head this snapshot is of and when the writing run started.

    ``failed``: this run's refresh of that head failed at that time (failed_refresh_body), the rest is older data.
    ``requeued``: a re-refresh of that head was dispatched at that time; no later failure of it queues another.
    """
    tail = f" failed={_stamp(failed)}" if failed else ""
    tail += f" requeued={_stamp(requeued)}" if requeued else ""
    return f"{STATE_PREFIX}head={head_sha} started={_stamp(started)} run={run_id}{tail} -->"


def with_state(body: str, head_sha: str, started: datetime, run_id: str = "", failed: datetime | None = None,
               requeued: datetime | None = None) -> str:
    """``body`` with the state line right after the marker line (the marker must stay first: find_comment_id)."""
    first, _, rest = body.partition("\n")
    if _STATE_RE.match(rest):  # replace an existing state line, never stack a second one
        rest = rest.partition("\n")[2]
    return f"{first}\n{state_line(head_sha, started, run_id, failed, requeued)}\n{rest}"


def requeued_at(existing_body: str, head_sha: str) -> datetime | None:
    """When a re-refresh of ``head_sha`` was queued, per the comment's state line (None: none for this head)."""
    m = _STATE_RE.search(existing_body or "")
    if not m or m.group(1) != head_sha or not m.group(5):
        return None
    return datetime.strptime(m.group(5), _STAMP).replace(tzinfo=UTC)


def stale_write_reason(existing_body: str, head_sha: str, started: datetime) -> str:
    """Why this run must not overwrite ``existing_body``, or "" when it may write.

    A snapshot of another head (or a comment with no state line, e.g. from before this marker) is always
    replaced. A snapshot of the same head is replaced only by a run that started later.
    """
    m = _STATE_RE.search(existing_body or "")
    if not m or m.group(1) != head_sha:
        return ""
    if _stamp(started) > m.group(2):
        return ""
    return (f"a run that started {m.group(2)} (run {m.group(3) or '?'}) already wrote head "
            f"{head_sha[:12]}; this run started {_stamp(started)}")


def find_bot_comment(token: str, repo: str, number: int) -> tuple[int | None, str]:
    """(id, body) of the PR's ci-review-bot comment, or (None, "")."""
    owner, name = repo.split("/")
    comments = lc._api_get_paginated(f"{lc.API_BASE}/repos/{owner}/{name}/issues/{number}/comments", token)
    for c in comments:
        body = c.get("body", "") if isinstance(c, dict) else ""
        if body.startswith(BOT_MARKER):
            return c.get("id"), body
    return None, ""


def build_snapshot(token: str, repo: str, pr: dict, run: dict, now: datetime,
                   retry_delays: tuple[float, ...] = BATCH_DOWNLOAD_RETRY_DELAYS,
                   deadline: float | None = None) -> str:
    watch = lc.parse_watch_workflows(os.environ.get("WATCH_WORKFLOWS", "")) or DEFAULT_WATCH_WORKFLOWS
    jobs, runs_completed = lc.collect_run_jobs(token, repo, str(run["id"]), watch, strict=True)
    completed, pending, job_urls = lc.classify_jobs(jobs)
    by_check, check_urls = lc.split_failed_steps(jobs, completed, job_urls)
    statuses = lc.fetch_all_review_statuses(token, repo, str(run["id"]), strict=True,
                                            retry_delays=retry_delays, sleep=_sleep,
                                            deadline=deadline, clock=_clock)
    return lc.build_comment_body(
        lc._import_assembler(),
        by_check,
        pending,
        run.get("html_url", ""),
        check_urls,
        json.dumps(statuses) if statuses else "",
        snapshot_commit_info(pr, run, repo, now, completed=runs_completed),
        waiting=not runs_completed,
    )


def process_pr(token: str, repo: str, pr: dict, now: datetime, *, trigger_workflow: str = "",
               retry_delays: tuple[float, ...] = BATCH_DOWNLOAD_RETRY_DELAYS, deadline: float | None = None) -> str:
    """Returns 'posted' or 'skipped:<why>'. Raises on API/write failure (SnapshotError when the snapshot itself
    could not be built, so nothing was written).

    ``now`` is this run's start; it is recorded in the comment's state line and orders competing writers.
    """
    owner, name = repo.split("/")
    number = pr["number"]
    sha = pr["head"]["sha"]
    runs = lc._api_get_paginated(
        f"{lc.API_BASE}/repos/{owner}/{name}/actions/runs"
        f"?event=pull_request&head_sha={sha}&per_page=100",
        token, list_key="workflow_runs",
    )
    run = select_ci_run(runs, sha)
    if run is None:
        return "skipped:no CI run on head"
    if run.get("status") == "completed" and run.get("conclusion") not in {"success", "failure"}:
        return "skipped:CI cancelled or incomplete"
    if trigger_workflow and trigger_workflow != CI_WORKFLOW_NAME and run.get("status") != "completed":
        # A watched sibling (Docker) finished first; CI's own completion on this head refreshes the PR.
        return f"skipped:{trigger_workflow} completed but CI is still {run.get('status') or 'unknown'}"
    try:
        body = build_snapshot(token, repo, pr, run, now, retry_delays, deadline)
    except (OSError, ValueError, RuntimeError) as e:
        raise SnapshotError(str(e)) from e

    # Head race: re-read the PR right before writing.
    fresh = lc._api_request(f"{lc.API_BASE}/repos/{owner}/{name}/pulls/{number}", token)
    if fresh.get("state") != "open":
        return "skipped:PR no longer open"
    if (fresh.get("head") or {}).get("sha") != sha:
        return "skipped:head changed"

    # Writer race: the hourly sweep and a targeted run can hold snapshots of the same head; the later-started wins.
    comment_id, existing = find_bot_comment(token, repo, number)
    why = stale_write_reason(existing, sha, now)
    if why:
        return f"skipped:newer snapshot present ({why})"
    body = with_state(body, sha, now, os.environ.get("GITHUB_RUN_ID", ""))
    if not lc.upsert_comment(token, repo, str(number), body, comment_id):
        raise RuntimeError("comment write failed")
    return "posted"


def failed_footer(pr: dict, sha: str, repo: str, failed_at: datetime, error: str, next_refresh: str) -> str:
    """The footer a failed refresh leaves: names the head (for readers) but not as "running"/"ran". Ship
    (hermes-core-wt-land BOT_HEAD) reads "refresh failed on <sha>" as naming that head for judging kept warning
    tables, never as the final "ran on" it waits for, so keep the wording."""
    url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/pull/{pr['number']}/commits/{sha}"
    err = " ".join(str(error).split())[:160]
    return (f"<sub>refresh failed on [{sha[:7]}]({url}) at {failed_at:%Y-%m-%d %H:%M:%S} UTC ({err}); "
            f"{next_refresh}; the snapshot above is older</sub>")


def keeps_completed_snapshot(existing: str, sha: str) -> bool:
    """``existing`` is a completed ("ran on") snapshot of ``sha`` with no failed mark: a failed refresh leaves it."""
    m = _STATE_RE.search(existing or "")
    if not m or m.group(1) != sha or m.group(4):
        return False
    found = _FOOTER_RE.search(existing)
    return bool(found and found.group(0).startswith("<sub>ran on "))


def failed_refresh_body(existing: str, pr: dict, sha: str, repo: str, started: datetime, run_id: str,
                        failed_at: datetime, error: str, next_refresh: str,
                        requeued: datetime | None = None) -> str | None:
    """The comment after this run's refresh of ``sha`` failed, or None to leave ``existing`` as it is.

    A completed ("ran on") snapshot of the same head is kept: it is still that head's latest result. A running or
    failed snapshot of the same head keeps its tables and gets the failed footer. Anything else (another head, no
    comment) is replaced by a short failed note, since its tables describe another head. ``requeued``: when a
    re-refresh of ``sha`` was queued (recorded in the state line, see mark_refresh_failed).
    """
    if keeps_completed_snapshot(existing, sha):
        return None
    footer = failed_footer(pr, sha, repo, failed_at, error, next_refresh)
    m = _STATE_RE.search(existing or "")
    if m and m.group(1) == sha:
        found = _FOOTER_RE.search(existing)
        body = (existing[:found.start()] + footer + existing[found.end():]) if found else f"{existing.rstrip()}\n\n{footer}\n"
    else:
        body = (f"{BOT_MARKER}\n## CI review summary\n\nThe CI review snapshot for this head could not be built.\n\n"
                f"{footer}\n")
    return with_state(body, sha, started, run_id, failed=failed_at, requeued=requeued)


def queue_re_refresh(token: str, repo: str, sha: str, run_id: str) -> datetime:
    """Dispatch this workflow once more for ``sha`` after RE_REFRESH_DELAY_S; returns when it will run."""
    owner, name = repo.split("/")
    ref = os.environ.get("DEFAULT_BRANCH", "").strip() or "main"
    payload = {"ref": ref, "inputs": {"head_sha": sha, "delay_seconds": str(RE_REFRESH_DELAY_S), "retry_of": run_id}}
    _api_post(f"{lc.API_BASE}/repos/{owner}/{name}/actions/workflows/{WORKFLOW_FILE}/dispatches", token, payload)
    return datetime.now(UTC) + timedelta(seconds=RE_REFRESH_DELAY_S)


def _api_post(url: str, token: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ci-review-comment",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"POST {url.rsplit('/', 3)[-3]} HTTP {e.code}") from e


def mark_refresh_failed(token: str, repo: str, pr: dict, now: datetime, error: str, *, re_refresh: bool) -> str:
    """After a targeted refresh could not build ``pr``'s snapshot: mark the comment failed and queue at most one
    delayed re-refresh per head. Returns a log line.

    ``re_refresh``: this is a CI- or Docker-triggered run, never the re-refresh itself. Nothing is queued when the
    marker is skipped (PR closed, head moved, a newer snapshot of the head, or a completed "ran on" snapshot of the
    head kept: that head already has its result), nor when the comment records a re-refresh already queued for this
    head (``requeued=``; CI and Docker each trigger a refresh, PR #1699 review). A successful snapshot drops the mark.
    """
    owner, name = repo.split("/")
    number, sha = pr["number"], pr["head"]["sha"]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    fresh = lc._api_request(f"{lc.API_BASE}/repos/{owner}/{name}/pulls/{number}", token)
    if fresh.get("state") != "open" or (fresh.get("head") or {}).get("sha") != sha:
        return "marker skipped: PR closed or head moved; no re-refresh queued"
    comment_id, existing = find_bot_comment(token, repo, number)
    why = stale_write_reason(existing, sha, now)
    if why:
        return f"marker skipped: newer snapshot present ({why}); no re-refresh queued"
    if keeps_completed_snapshot(existing, sha):
        return "marker skipped: completed snapshot of this head kept; no re-refresh queued"
    notes = []
    requeued = requeued_at(existing, sha)
    if not re_refresh:  # the re-refresh itself (or a sweep): the mark is carried, the hourly sweep is next
        next_refresh = "next refresh: hourly sweep"
    elif requeued:
        next_refresh = f"a re-refresh already queued at {requeued:%H:%M} UTC, then hourly"
        notes.append(f"re-refresh already queued at {requeued:%H:%M:%S}Z; not queued again")
    else:
        try:
            at = queue_re_refresh(token, repo, sha, run_id)
            requeued = datetime.now(UTC)
            next_refresh = f"one re-refresh queued for about {at:%H:%M} UTC, then hourly"
            notes.append(f"re-refresh queued for {at:%H:%M:%S}Z")
        except (OSError, ValueError, RuntimeError) as e:
            next_refresh = "next refresh: hourly sweep"
            notes.append(f"ERROR queueing re-refresh: {e}")
    body = failed_refresh_body(existing, pr, sha, repo, now, run_id, datetime.now(UTC), error, next_refresh,
                               requeued=requeued)
    if body is None:  # unreachable: keeps_completed_snapshot was checked above
        return "; ".join([*notes, "marker skipped: completed snapshot of this head kept"])
    if not lc.upsert_comment(token, repo, str(number), body, comment_id):
        return "; ".join([*notes, "ERROR marker write failed"])
    return "; ".join([*notes, "comment marked refresh failed"])


def run_batch(token: str, repo: str, now: datetime | None = None, *, head_sha: str = "",
              trigger_workflow: str = "", deadline: float | None = None) -> int:
    """Snapshot every eligible open PR, or only those whose head is exactly ``head_sha``.

    ``head_sha`` is the targeted mode the workflow uses on ``workflow_run``: the
    CI run on that head just completed, so only its PR's comment is refreshed
    (no other PR's comment is rewritten). ``trigger_workflow`` names the
    workflow whose completion started this run (empty on schedule/dispatch).
    ``deadline`` (a ``_clock()`` value, see RUN_BUDGET_S): no PR is started, and no download retried, past it.
    """
    now = now or datetime.now(UTC)
    owner, name = repo.split("/")
    try:
        prs = lc._api_get_paginated(
            f"{lc.API_BASE}/repos/{owner}/{name}/pulls?state=open&per_page=100", token,
        )
    except (OSError, ValueError, RuntimeError) as e:
        print(f"ERROR listing open PRs: {e}", file=sys.stderr)
        return 1

    targets = eligible_prs(prs, repo)
    print(f"{len(prs)} open PR(s), {len(targets)} same-repo")
    if head_sha:
        targets = [pr for pr in targets if pr["head"]["sha"] == head_sha]
        print(f"targeted at head {head_sha}: {len(targets)} PR(s)")
    delays = TARGETED_DOWNLOAD_RETRY_DELAYS if head_sha else BATCH_DOWNLOAD_RETRY_DELAYS
    failures = 0
    for i, pr in enumerate(targets):
        if deadline is not None and _clock() + lc.DOWNLOAD_ATTEMPT_MAX_S + PR_MIN_BUDGET_S > deadline:
            left = len(targets) - i
            failures += left
            print(f"ERROR run budget ({RUN_BUDGET_S}s) spent: {left} PR(s) not refreshed "
                  f"(#{', #'.join(str(p['number']) for p in targets[i:])}); the next sweep takes them",
                  file=sys.stderr)
            break
        try:
            result = process_pr(token, repo, pr, now, trigger_workflow=trigger_workflow, retry_delays=delays,
                                deadline=deadline)
            print(f"  PR #{pr['number']}: {result}")
        except SnapshotError as e:
            failures += 1
            print(f"  PR #{pr['number']}: ERROR {e}", file=sys.stderr)
            if head_sha:
                # The sweep stays as it was (the next sweep retries); a targeted refresh must not leave "running on".
                try:
                    note = mark_refresh_failed(token, repo, pr, now, str(e), re_refresh=bool(trigger_workflow))
                except (OSError, ValueError, RuntimeError) as e2:
                    note = f"ERROR marking the failed refresh: {e2}"
                print(f"  PR #{pr['number']}: {note}", file=sys.stderr)
        except (OSError, ValueError, RuntimeError) as e:
            failures += 1
            print(f"  PR #{pr['number']}: ERROR {e}", file=sys.stderr)
    if failures:
        print(f"{failures} PR(s) failed", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--head-sha",
                   default=os.environ.get("TARGET_HEAD_SHA", "") or os.environ.get("DISPATCH_HEAD_SHA", ""),
                   help="only the open PR(s) whose head is exactly this sha (default: $TARGET_HEAD_SHA, else the "
                        "dispatch input $DISPATCH_HEAD_SHA; empty = every open same-repo PR)")
    a = p.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 1
    head_sha = a.head_sha.strip().lower()
    deadline = _clock() + RUN_BUDGET_S  # from the start: a re-refresh's delay spends the same budget
    if head_sha and not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        print(f"--head-sha must be a full 40-hex commit sha, got {a.head_sha!r}", file=sys.stderr)
        return 1
    # The workflow_run trigger's workflow name (CI or a watched sibling); empty on schedule/dispatch.
    trigger_workflow = os.environ.get("TRIGGER_WORKFLOW", "").strip()
    # A queued re-refresh (workflow_dispatch with head_sha) waits first, so a transient artifact error can clear.
    delay = os.environ.get("REFRESH_DELAY_SECONDS", "").strip()
    if head_sha and delay:
        if not delay.isdigit():
            print(f"REFRESH_DELAY_SECONDS must be a whole number of seconds, got {delay!r}", file=sys.stderr)
            return 1
        wait = min(int(delay), MAX_REFRESH_DELAY_S)
        if wait:
            print(f"re-refresh of {head_sha[:12]} (retry of run {os.environ.get('RETRY_OF_RUN', '') or '?'}): "
                  f"waiting {wait}s")
            _sleep(wait)
    return run_batch(token, repo, head_sha=head_sha, trigger_workflow=trigger_workflow, deadline=deadline)


if __name__ == "__main__":
    sys.exit(main())
