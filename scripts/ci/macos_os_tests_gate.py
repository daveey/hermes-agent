#!/usr/bin/env python3
"""Decide whether a scheduled macOS OS-marked test run may take the mini.

Called by .github/workflows/macos-os-tests.yml before its job on the Mac mini's
self-hosted runner (labels self-hosted, macOS, hermes-state). That one runner
also serves the required hermes_state (macOS) and Native memory lanes of pull
requests, and it is already saturated by them. So the scheduled run yields:
when any job for the mini's label is waiting for a runner (queued, waiting,
pending or requested) in another workflow run, this run skips, and the next
schedule slot tries again. A job that is already running on the mini is not a
reason to skip: ours queues behind one job, it does not starve a queue.

Only ``schedule`` runs are gated. A manual dispatch or a probe push always runs.
If the API cannot be read, the run goes ahead (fail open to testing): a
transient read error should not silently drop the lane, and one run's extra
load is bounded by the workflow's concurrency group.

Writes ``run=true|false`` to $GITHUB_OUTPUT. Standard library only; the GitHub
client shells out to ``gh api`` with an argument vector, using GH_TOKEN.

Tests: tests/ci/test_macos_os_tests_workflow.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

MINI_LABEL = "hermes-state"
WAITING = frozenset({"queued", "waiting", "pending", "requested"})
OPEN_RUN_STATES = ("queued", "in_progress", "waiting", "pending", "requested")

PAGE_SIZE = 100
MAX_RUN_PAGES = 10

Api = Callable[[str], dict[str, Any]]


class QueueSaturated(Exception):
    """More open runs in one state than the gate reads: the mini is busy."""


def waiting_mini_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Jobs that want the mini's label and have no runner yet."""
    return [
        job for job in jobs
        if job.get("status") in WAITING and MINI_LABEL in (job.get("labels") or [])
    ]


def find_waiting(api: Api, repo: str, own_run_id: int) -> list[dict[str, Any]]:
    """Every waiting mini job across this repository's open runs, ours excluded.

    Busy hours have 100+ open runs, so each state's listing is read page by
    page until a short page; a waiting job on page 2 counts like one on page 1.
    Past MAX_RUN_PAGES full pages the queue is saturated by any measure
    (QueueSaturated: skip). Job lists are read 8 at a time (read serially,
    one probe took ~3 min).
    """
    run_ids: list[int] = []
    for state in OPEN_RUN_STATES:
        for page in range(1, MAX_RUN_PAGES + 1):
            path = f"repos/{repo}/actions/runs?status={state}&per_page={PAGE_SIZE}&page={page}"
            runs = api(path).get("workflow_runs") or []
            for run in runs:
                run_id = int(run["id"])
                if run_id != own_run_id and run_id not in run_ids:
                    run_ids.append(run_id)
            if len(runs) < PAGE_SIZE:
                break
        else:
            raise QueueSaturated(f"{MAX_RUN_PAGES * PAGE_SIZE}+ {state} runs")

    def jobs(run_id: int) -> list[dict[str, Any]]:
        return api(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100").get("jobs") or []

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [job for listed in pool.map(jobs, run_ids) for job in waiting_mini_jobs(listed)]


def decide(event: str, api: Api, repo: str, own_run_id: int) -> tuple[bool, str]:
    if event != "schedule":
        return True, f"{event} run: not gated"
    try:
        waiting = find_waiting(api, repo, own_run_id)
    except QueueSaturated as exc:
        return False, f"{exc} open; skipping this slot"
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as exc:
        # Any read failure (gh error/timeout, bad JSON, missing field) fails open to testing.
        return True, f"could not read the mini's queue ({exc}); running anyway"
    if waiting:
        names = ", ".join(sorted({str(job.get("name")) for job in waiting}))
        return False, f"{len(waiting)} job(s) already waiting for the mini ({names}); skipping this slot"
    return True, "no job waiting for the mini"


def gh_api(path: str) -> dict[str, Any]:
    argv = ["gh", "api", path]
    out = subprocess.run(argv, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return json.loads(out.stdout)


def main() -> int:
    run, reason = decide(
        os.environ.get("GITHUB_EVENT_NAME", ""),
        gh_api,
        os.environ["GITHUB_REPOSITORY"],
        int(os.environ.get("GITHUB_RUN_ID", "0")),
    )
    print(("::notice::" if run else "::warning::") + reason)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
