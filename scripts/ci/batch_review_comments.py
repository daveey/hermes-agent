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
    replaces earlier comments from the live poller. The footer says the
    snapshot is refreshed hourly, so it is not mistaken for a live view.
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
        f"refreshed hourly, not live</sub>"
    )


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


def process_pr(token: str, repo: str, pr: dict, now: datetime) -> str:
    """Returns 'posted' or 'skipped:<why>'. Raises on API/write failure."""
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
    body = build_snapshot(token, repo, pr, run, now)

    # Head race: re-read the PR right before writing.
    fresh = lc._api_request(f"{lc.API_BASE}/repos/{owner}/{name}/pulls/{number}", token)
    if fresh.get("state") != "open":
        return "skipped:PR no longer open"
    if (fresh.get("head") or {}).get("sha") != sha:
        return "skipped:head changed"

    if not lc.upsert_comment(token, repo, str(number), body):
        raise RuntimeError("comment write failed")
    return "posted"


def run_batch(token: str, repo: str, now: datetime | None = None, *, head_sha: str = "") -> int:
    """Snapshot every eligible open PR, or only those whose head is exactly ``head_sha``.

    ``head_sha`` is the targeted mode the workflow uses on ``workflow_run``: the
    CI run on that head just completed, so only its PR's comment is refreshed
    (no other PR's comment is rewritten).
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
            result = process_pr(token, repo, pr, now)
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
    return run_batch(token, repo, head_sha=head_sha)


if __name__ == "__main__":
    sys.exit(main())
