#!/usr/bin/env python3
"""Why an upstream sync candidate's full suite went red: evidence printed under the gate's REFUSED line.

Never a gate input: ``upstream_sync.advance`` has already decided REFUSED before this runs, and any failure here
prints "diagnostics unavailable" without touching that verdict.

For each failed required job of the candidate run it prints the job name, its URL and the failing test ids parsed
from its log (``nightly_e2e.failing_tests``). It then compares them with main at the candidate's fork point (the
merge-base with origin/main, i.e. the main the sync was built on) using only runs that tested exactly that sha:

* ci.yaml runs on main at that sha (Python slices; e2e only when that run had the e2e lane),
* nightly-e2e.yml runs whose "Resolve ref" job logged ``Testing <ref> = <that sha>`` (a nightly's head_sha is the
  workflow's commit, not the tested one, so head_sha alone is not evidence).

A failing id that also failed there is a main-baseline failure: upstream did not cause it. An id is a candidate-only
failure only when a complete run of that job kind at the base (every job finished, full Python slices, every failed
job's ids parsed) did not fail it; that is an observation, not proof that upstream caused it (tests flake). Anything
else is "baseline comparison unknown". The e2e failing set is then matched against the nightly reporter's cards
(``~/.hermes/state/nightly-e2e.json``, key = ``nightly_e2e.failure_key``): a card is named only when it is open, was
created by nightly-e2e and carries that exact key.
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flaky_kanban import OPEN
from nightly_e2e import DEFAULT_STATE, SETTLE_ERRORS, NightlyBoard, failure_key, failing_tests, load_state

CI_WORKFLOW = ".github/workflows/ci.yaml"
NIGHTLY_WORKFLOW = ".github/workflows/nightly-e2e.yml"
NIGHTLY_RUNS_CHECKED = 10
TESTED_SHA = re.compile(r"Z Testing \S+ = ([0-9a-f]{40})\s*$", re.MULTILINE)
SLICE = re.compile(r"^Run tests \((affected, )?\d+/\d+\)$")
LISTED = 20


def job_kind(name: str) -> str | None:
    """'e2e' or 'python' for a tests.yml job name (``Full suite / e2e``, ``Python tests / Run tests (2/8)``)."""
    short = str(name).rsplit(" / ", 1)[-1]
    if short == "e2e":
        return "e2e"
    return "python" if SLICE.match(short) else None


@dataclass
class JobEvidence:
    name: str
    conclusion: str
    url: str
    ids: list[str] = field(default_factory=list)
    ids_known: bool = True  # a failed job whose log was read and named at least one test


@dataclass
class BaselineEvidence:
    url: str
    jobs: list[JobEvidence]
    complete: bool  # every job of the kind finished success/failure, full slices: a non-failure means it passed

    @property
    def failed_ids(self) -> set[str]:
        return {i for job in self.jobs for i in job.ids}

    @property
    def ids_complete(self) -> bool:
        return all(job.ids_known for job in self.jobs if job.conclusion != "success")


def _jobs(gh_repo, run_id, request) -> list[dict]:
    return [job for page in request(f"repos/{gh_repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest")
            for job in page["jobs"]]


def _log(gh_repo, job_id, gh) -> str | None:
    proc = gh("api", f"repos/{gh_repo}/actions/jobs/{job_id}/logs", reason="upstream sync gate: read a failed job log")
    return proc.stdout if proc.returncode == 0 else None


def _evidence(gh_repo, job, gh) -> JobEvidence:
    ev = JobEvidence(job["name"], str(job.get("conclusion") or job.get("status")), job.get("html_url") or "")
    if ev.conclusion == "success":
        return ev
    log = _log(gh_repo, job["id"], gh) if ev.conclusion == "failure" else None
    ev.ids = failing_tests(log or "")
    ev.ids_known = bool(ev.ids)
    return ev


def candidate_failures(gh_repo, run_id, request, gh, required) -> list[JobEvidence]:
    jobs = {job["name"].rsplit(" / ", 1)[-1]: job for job in _jobs(gh_repo, run_id, request)}
    return [_evidence(gh_repo, jobs[name], gh) for name in required
            if name in jobs and jobs[name].get("conclusion") != "success"]


def _tested_sha(gh_repo, run_id, request, gh) -> str | None:
    resolve = [job for job in _jobs(gh_repo, run_id, request) if job.get("name") == "Resolve ref"]
    log = _log(gh_repo, resolve[0]["id"], gh) if resolve else None
    found = TESTED_SHA.findall(log or "")
    return found[-1] if found else None


def baseline_runs(gh_repo, base, base_time: str, request, gh) -> list[dict]:
    """Completed runs that tested exactly ``base`` on main, newest first (``base_time``: its commit time, UTC)."""
    runs = [r for page in request(f"repos/{gh_repo}/actions/runs?head_sha={base}&per_page=100")
            for r in page["workflow_runs"]]
    found = [r for r in runs if r.get("path") == CI_WORKFLOW and r.get("head_sha") == base
             and r.get("head_branch") == "main" and r.get("event") in ("push", "workflow_dispatch")]
    # Only nightlies created after the base commit can have tested it; the newest few are read.
    nightly = [r for page in request(f"repos/{gh_repo}/actions/workflows/nightly-e2e.yml/runs?event=workflow_dispatch"
                                     f"&status=completed&created=%3E%3D{base_time}&per_page=100")
               for r in page["workflow_runs"]]
    nightly = sorted((r for r in nightly if r.get("path") == NIGHTLY_WORKFLOW and r.get("event") == "workflow_dispatch"
                      and r.get("status") == "completed"), key=lambda r: r["id"], reverse=True)
    found += [run for run in nightly[:NIGHTLY_RUNS_CHECKED] if _tested_sha(gh_repo, run["id"], request, gh) == base]
    return sorted((r for r in found if r.get("status") == "completed"), key=lambda r: r["id"], reverse=True)


def baseline(gh_repo, runs, kind, request, gh) -> BaselineEvidence | None:
    """The newest of ``runs`` (baseline_runs) that ran ``kind`` jobs (not skipped), with verdicts and failing ids."""
    for run in runs:
        jobs = [j for j in _jobs(gh_repo, run["id"], request) if job_kind(j.get("name", "")) == kind]
        ran = [j for j in jobs if j.get("conclusion") in ("success", "failure")]
        if not ran:
            continue
        complete = len(ran) == len(jobs) and not any("affected" in j["name"] for j in jobs)
        return BaselineEvidence(run.get("html_url") or "", [_evidence(gh_repo, j, gh) for j in ran], complete)
    return None


def nightly_owner(ids, *, state_path: Path | None = None, board=None) -> str:
    """The open nightly-e2e card filed for exactly this failing set, else ''. Board errors raise."""
    state_path = Path(state_path or DEFAULT_STATE)
    if not ids or not state_path.exists():
        return ""
    key = failure_key(list(ids))
    rec = load_state(state_path)["cards"].get(key)
    if not rec:
        return ""
    card = (board or NightlyBoard("hermes")).card(rec["id"])
    if card is None:  # CliBoard.card answers None for a failed `hermes kanban show`: unknown, not "no owner"
        raise RuntimeError(f"nightly card {rec['id']} for {key} is unreadable on this host's board")
    if (card.get("status") in OPEN and card.get("created_by") == "nightly-e2e"
            and str(card.get("idempotency_key") or "").startswith(f"nightly-e2e:{key}:")):
        return f"{card['id']} ({card['status']})"
    return ""


def _listing(ids) -> str:
    shown = sorted(ids)[:LISTED]
    more = f"\n      … and {len(ids) - len(shown)} more" if len(ids) > len(shown) else ""
    return "".join(f"\n      - {i}" for i in shown) + more


def compare(kind, failed: list[JobEvidence], base: str, evidence: BaselineEvidence | None) -> list[str]:
    """Attribution lines for one job kind: main-baseline / candidate-only / unknown, each backed by a run URL."""
    ids = {i for job in failed for i in job.ids}
    known = all(job.ids_known for job in failed)
    short = base[:10]
    if evidence is None:
        return [f"  {kind}: baseline comparison unknown: no completed {kind} run tested main {short}"]
    pre = ids & evidence.failed_ids
    lines = []
    if pre:
        lines.append(f"  {kind}: main-baseline failure: {len(pre)} failing test(s) also fail on main {short} "
                     f"({evidence.url}); this is not evidence that upstream caused them:{_listing(pre)}")
    rest = ids - pre
    if rest and evidence.complete and evidence.ids_complete:
        lines.append(f"  {kind}: candidate-only failure observed: {len(rest)} test(s) did not fail on main {short} "
                     f"({evidence.url}); not proof of upstream causation (a test can flake):{_listing(rest)}")
    elif rest:
        lines.append(f"  {kind}: baseline comparison unknown for {len(rest)} test(s): main {short}'s run "
                     f"({evidence.url}) is incomplete (skipped/affected-only slices or unparsed failures)")
    if not known:
        state = "also failed" if any(j.conclusion == "failure" for j in evidence.jobs) else "passed"
        lines.append(f"  {kind}: baseline comparison unknown for a required job with no parsed test ids; main "
                     f"{short}'s {kind} {state} ({evidence.url})")
    return lines


def _owner_lines(candidate_ids, main_ids) -> list[str]:
    """The candidate's exact e2e set's owner; main's set's owner only when main fails a different set."""
    if not Path(DEFAULT_STATE).exists():
        return [f"  nightly owner lookup unavailable: no nightly-e2e state on this host ({DEFAULT_STATE}; "
                "it runs on hermes-hub)"]
    try:
        exact = nightly_owner(candidate_ids)
        other = nightly_owner(main_ids) if main_ids and set(main_ids) != set(candidate_ids) else ""
    except SETTLE_ERRORS as exc:  # an unreadable board or state must not hide the CI evidence above
        return [f"  nightly owner lookup unavailable: {exc!r}"]
    lines = [f"  nightly-e2e owner for this exact failing set: {exact}" if exact
             else "  no open nightly-e2e card matches this exact e2e failing set"]
    if other:
        lines.append(f"  main's own e2e failing set (a different set) is owned by nightly-e2e card {other}")
    return lines


def fork_point(repo, head: str, main: str, git) -> tuple[str, str]:
    """(sha, how): the main this sync was built on. Its first parent when that is on main, else the merge-base."""
    first = git(repo, "rev-parse", f"{head}^1")
    if git(repo, "merge-base", first, main) == first:
        return first, "the sync's first parent, on main"
    return git(repo, "merge-base", head, main), "merge-base with main"


def explain(repo, cand, main: str, gh_repo: str, request, gh, *, required, git) -> None:
    """Print the evidence lines for a red candidate (module doc). Raises on unreadable CI; the caller catches."""
    failed = candidate_failures(gh_repo, cand.run_id, request, gh, required)
    if not failed:
        print("  no required job failed: the run lacked required self-hosted job evidence, see the run")
        return
    for job in failed:
        tests = f" — {len(job.ids)} failing test(s){_listing(job.ids)}" if job.ids else " — no failing test id parsed"
        print(f"  required job {job.conclusion}: {job.name} {job.url}{tests}")
    base, how = fork_point(repo, cand.head, main, git)
    print(f"  main baseline: {base[:10]} ({how})")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(git(repo, "show", "-s", "--format=%ct", base))))
    runs = baseline_runs(gh_repo, base, stamp, request, gh)
    for kind in ("python", "e2e"):
        of_kind = [job for job in failed if job_kind(job.name) == kind]
        if not of_kind:
            continue
        evidence = baseline(gh_repo, runs, kind, request, gh)
        for line in compare(kind, of_kind, base, evidence):
            print(line)
        if kind == "e2e":
            main_ids = sorted(evidence.failed_ids) if evidence else []
            for line in _owner_lines(sorted({i for j in of_kind for i in j.ids}), main_ids):
                print(line)
