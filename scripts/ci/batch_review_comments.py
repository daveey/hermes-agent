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
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

CI_WORKFLOW_NAME = "CI"
DEFAULT_WATCH_WORKFLOWS = ["Docker Build, Test, and Publish"]

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
    r"started=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z) run=(\S*) -->"
)


def _stamp(started: datetime) -> str:
    return started.astimezone(UTC).strftime(_STAMP)


def state_line(head_sha: str, started: datetime, run_id: str = "") -> str:
    """The hidden line recording which head this snapshot is of and when the writing run started."""
    return f"{STATE_PREFIX}head={head_sha} started={_stamp(started)} run={run_id} -->"


def with_state(body: str, head_sha: str, started: datetime, run_id: str = "") -> str:
    """``body`` with the state line right after the marker line (the marker must stay first: find_comment_id)."""
    first, _, rest = body.partition("\n")
    return f"{first}\n{state_line(head_sha, started, run_id)}\n{rest}"


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


def build_snapshot(token: str, repo: str, pr: dict, run: dict, now: datetime) -> str:
    watch = lc.parse_watch_workflows(os.environ.get("WATCH_WORKFLOWS", "")) or DEFAULT_WATCH_WORKFLOWS
    jobs, runs_completed = lc.collect_run_jobs(token, repo, str(run["id"]), watch, strict=True)
    completed, pending, job_urls = lc.classify_jobs(jobs)
    by_check, check_urls = lc.split_failed_steps(jobs, completed, job_urls)
    statuses = lc.fetch_all_review_statuses(token, repo, str(run["id"]), strict=True)
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


def process_pr(token: str, repo: str, pr: dict, now: datetime, *, trigger_workflow: str = "") -> str:
    """Returns 'posted' or 'skipped:<why>'. Raises on API/write failure.

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
    body = build_snapshot(token, repo, pr, run, now)

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


def run_batch(token: str, repo: str, now: datetime | None = None, *, head_sha: str = "",
              trigger_workflow: str = "") -> int:
    """Snapshot every eligible open PR, or only those whose head is exactly ``head_sha``.

    ``head_sha`` is the targeted mode the workflow uses on ``workflow_run``: the
    CI run on that head just completed, so only its PR's comment is refreshed
    (no other PR's comment is rewritten). ``trigger_workflow`` names the
    workflow whose completion started this run (empty on schedule/dispatch).
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
    failures = 0
    for pr in targets:
        try:
            result = process_pr(token, repo, pr, now, trigger_workflow=trigger_workflow)
            print(f"  PR #{pr['number']}: {result}")
        except (OSError, ValueError, RuntimeError) as e:
            failures += 1
            print(f"  PR #{pr['number']}: ERROR {e}", file=sys.stderr)
    if failures:
        print(f"{failures} PR(s) failed", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--head-sha", default=os.environ.get("TARGET_HEAD_SHA", ""),
                   help="only the open PR(s) whose head is exactly this sha (default: $TARGET_HEAD_SHA; "
                        "empty = every open same-repo PR)")
    a = p.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 1
    head_sha = a.head_sha.strip().lower()
    if head_sha and not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        print(f"--head-sha must be a full 40-hex commit sha, got {a.head_sha!r}", file=sys.stderr)
        return 1
    # The workflow_run trigger's workflow name (CI or a watched sibling); empty on schedule/dispatch.
    trigger_workflow = os.environ.get("TRIGGER_WORKFLOW", "").strip()
    return run_batch(token, repo, head_sha=head_sha, trigger_workflow=trigger_workflow)


if __name__ == "__main__":
    sys.exit(main())
