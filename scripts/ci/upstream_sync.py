#!/usr/bin/env python3
"""The only way an upstream sync reaches the fork's main: a full suite on the fork's runners first.

    python3 scripts/ci/upstream_sync.py --head REV      # stage, or land once green, one candidate
    python3 scripts/ci/upstream_sync.py                 # only land staged candidates that went green

A candidate X (a commit that brings upstream/main commits into the fork) is pushed to its own
branch ``sync/upstream-<X>``. ``.github/workflows/upstream-sync.yml`` runs the full suite there
(all eight Python slices and e2e, self-hosted, never cancelled by main's concurrency). Only an X whose
run completed green, with every required job succeeding on a self-hosted runner at exactly X, is
landed through a pull request from that same branch. main is protected for admins too: GitHub
refuses any direct push whose exact sha lacks a green "All required checks pass (v2)" (ci.yaml).
The gate opens (or reuses) the PR, waits across calls until that check is green on exactly X, then
runs ``gh pr merge --merge --match-head-commit X`` (never --admin). Nothing is ever force-pushed.
Red, pending or unreadable evidence is a refusal. A protection refusal is reported as REFUSED
with GitHub's own message, never as "main moved". A red full suite's REFUSED line is followed by
evidence lines (scripts/ci/upstream_sync_diagnostics.py): each failed job, its failing test ids,
whether main at the sync's fork point already fails them, and the matching nightly-e2e card. They
never change the verdict.

Each call also lands any earlier staged candidate that went green (hosts that merged upstream at
different times converge on one tested sync), keeps at most one candidate in CI, and stops staging
for RED_BACKOFF_S after a red run so a broken upstream does not loop full suites. Last, every call closes
open sync PRs whose head's parents main already contains (scripts/ci/upstream_sync_superseded.py), with
a comment naming that main, and lease-deletes superseded staging branches without open PRs.
This call's staged/landing candidates are protected; cleanup faults never change the exit code.

Exit codes: 0 landed (or already on main), 1 refused (red, conflict, protection refusal, error),
2 not yet (staged, pending, throttled, landing PR waiting for its required check): run it again
with the same head after CI finishes.

The pre-push hook (``scripts/check --install-hook pre-push``, through ``scripts/check --commit``) runs
``guard_push``: a push of any branch that brings upstream commits the fork's main lacks is refused
unless this gate is the one pushing.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

WORKFLOW_FILE = "upstream-sync.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
BRANCH_PREFIX = "sync/upstream-"
# The job names tests.yml gives the full suite (called as job "Full suite"); all must succeed.
REQUIRED_JOBS = (*(f"Run tests ({n}/8)" for n in range(1, 9)), "e2e")
RED_BACKOFF_S = 6 * 3600
STALE_PENDING_S = 6 * 3600  # a run queued this long never got runners: it no longer holds the slot
# Set on the gate's own pushes; the pre-push guard lets exactly those through.
GATE_ENV = "HERMES_UPSTREAM_SYNC_GATE"
UPSTREAM_REF = "refs/remotes/upstream/main"

LANDED, REFUSED, NOT_YET = 0, 1, 2
# The check daveey/hermes main's branch protection requires on a PR head (ci.yaml's aggregate job),
# bound to the GitHub Actions app. enforce_admins is on: no token lands main without it.
REQUIRED_CHECK = "All required checks pass (v2)"
ACTIONS_APP_ID = 15368
# GitHub's words for a protection refusal (push: GH006/GH013; merge API: base branch policy, rulesets).
PROTECTION_MARKS = ("gh006", "gh013", "protected branch", "required status check", "repository rule violation",
                    "base branch policy", "branch protection")
# A merge refused for a race GitHub resolves by itself: try again on the next call.
MERGE_RETRY_MARKS = ("base branch was modified", "merge already in progress")


class GateError(Exception):
    pass


class Refused(GateError):
    """GitHub refused a ref update (branch protection or a ruleset). Carries GitHub's message."""


def is_protection_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(mark in low for mark in PROTECTION_MARKS)


def _run(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=600, check=False)


def git(repo, *args, env=None) -> str:
    proc = _run(repo, *args, env=env)
    if proc.returncode:
        raise GateError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout.strip()


def _is_ancestor(repo, sha, of) -> bool:
    return _run(repo, "merge-base", "--is-ancestor", sha, of).returncode == 0


def _gate_env(stage: str) -> dict:
    return {**os.environ, GATE_ENV: stage, "GIT_TERMINAL_PROMPT": "0"}


def gh_cli(*args, reason="upstream sync gate: read fork CI evidence") -> subprocess.CompletedProcess:
    """Run ``gh *args``: the logged-in gh, else again under the broker's ``user/github`` scope."""
    # This standalone gate also runs before the core environment is activated, under systemd's PATH.
    install_dirs = (Path.home() / "bin", Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
    gh = shutil.which("gh") or shutil.which("gh", path=os.pathsep.join(map(str, install_dirs)))
    argv = [gh or "gh", *args]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    except FileNotFoundError:
        # A broker may supply its own PATH; missing locally must not skip that recovery path.
        proc = subprocess.CompletedProcess(argv, 127, "", "gh not found in PATH or known install directories")
    broker = shutil.which("token-broker") or str(Path.home() / ".local/bin/token-broker")
    if proc.returncode and Path(broker).exists():
        proc = subprocess.run([broker, "exec", "--scope", "user/github", "--reason", reason, "--", *argv],
                              encoding="utf-8", capture_output=True, text=True, timeout=180, check=False)
    return proc


def github_pages(endpoint):
    """Every page of a GitHub API GET: the logged-in gh, else the broker's ``user/github`` scope."""
    proc = gh_cli("api", "--paginate", "--slurp", endpoint)
    if proc.returncode:
        raise GateError(f"GitHub API read failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return json.loads(proc.stdout)


@dataclass
class Candidate:
    head: str
    status: str  # green | red | pending
    url: str
    updated_at: float
    run_id: int = 0  # the run the verdict came from: diagnostics read its jobs


def _ts(value) -> float:
    return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) if value else 0.0


def _jobs_green(gh_repo, run_id, request) -> bool:
    pages = request(f"repos/{gh_repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest")
    jobs = {job["name"].rsplit(" / ", 1)[-1]: job for page in pages for job in page["jobs"]}
    return all(name in jobs and jobs[name]["status"] == "completed"
               and jobs[name]["conclusion"] == "success"
               and "self-hosted" in (jobs[name].get("labels") or []) and jobs[name].get("runner_name")
               for name in REQUIRED_JOBS)


def candidates(gh_repo, request) -> dict[str, Candidate]:
    """Each staged candidate's newest verdict, keyed by head sha."""
    pages = request(f"repos/{gh_repo}/actions/workflows/{WORKFLOW_FILE}/runs?event=push&per_page=50")
    runs = sorted((r for page in pages for r in page["workflow_runs"]), key=lambda r: (r.get("run_number", 0), r["id"]))
    found: dict[str, Candidate] = {}
    for run in runs:  # oldest first: a later run on the same head replaces the earlier verdict
        head = run["head_sha"]
        if run["head_branch"] != BRANCH_PREFIX + head or run["path"] != WORKFLOW_PATH or run["event"] != "push":
            continue  # evidence counts only from the candidate's own branch and this workflow
        if run["status"] != "completed":
            status = "pending"
        elif run["conclusion"] == "success":
            status = "green" if _jobs_green(gh_repo, run["id"], request) else "red"
        elif run["conclusion"] in ("cancelled", "skipped", "stale"):
            continue  # never reached a verdict: neither evidence nor a failure
        else:
            status = "red"
        found[head] = Candidate(head, status, run["html_url"], _ts(run.get("updated_at") or run.get("created_at")),
                                run["id"])
    return found


def _fetch_main(repo) -> str:
    git(repo, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
    return git(repo, "rev-parse", "refs/remotes/origin/main")


def _parents_on_main(repo, head, main) -> bool:
    """A merge whose parents already landed is superseded, even with unique resolution content."""
    result = _run(repo, "rev-list", "--parents", "-n1", head)
    parents = result.stdout.split()[1:]
    return result.returncode == 0 and len(parents) >= 2 and all(_is_ancestor(repo, p, main) for p in parents)


def _adds_nothing(repo, head, main) -> bool:
    """Merging ``head`` into ``main`` changes no file (a twin of a sync that already landed)."""
    tree = _run(repo, "merge-tree", "--write-tree", "--no-messages", main, head)
    return tree.returncode == 0 and tree.stdout.split("\n", 1)[0].strip() == git(repo, "rev-parse", f"{main}^{{tree}}")


def _push(repo, src: str, ref: str, stage: str) -> None:
    """Push ``src`` to ``ref`` as the gate. A protection refusal raises Refused with GitHub's words."""
    push = _run(repo, "push", "-q", "origin", f"{src}:{ref}", env=_gate_env(stage))
    if push.returncode:
        lines = [line.strip() for line in (push.stderr or push.stdout or "").splitlines() if line.strip()]
        remote = [line for line in lines if line.startswith("remote:")]
        # GitHub's verdict lines first (``remote: error: GH006 …``): the tail is paths and hints
        message = " | ".join([line for line in remote if "error" in line.lower()] or remote or lines)[:600]
        if is_protection_refusal(message):
            raise Refused(f"GitHub refused the push to {ref}: {message}")
        raise GateError(f"git push to {ref} failed: {message}")


def _remote_sha(repo, ref: str) -> str:
    out = git(repo, "ls-remote", "origin", ref)
    return out.split()[0] if out else ""


def _land_pr(repo, cand: Candidate, gh_repo, request, gh) -> dict:
    """The open PR main <- sync/upstream-<head> whose head is exactly the tested head; opened if absent.

    The PR comes from the candidate's own staging branch, so its head IS the sha the full suite
    passed on: nothing new is built, and ci.yaml's pull_request run puts the required check on it.
    """
    branch = BRANCH_PREFIX + cand.head
    remote = _remote_sha(repo, f"refs/heads/{branch}")
    if remote and remote != cand.head:
        raise GateError(f"{branch} now points at {remote[:10]}, not the tested head {cand.head[:10]}")
    if not remote:  # the staged branch went away: put the tested head back (same sha, same evidence)
        _push(repo, cand.head, f"refs/heads/{branch}", "stage")
    # Match the head ref, not ``head=<owner>:<branch>``: origin's slug may name a renamed/transferred repo
    # (daveey/hermes -> softmax-universe/daveey-hermes), so the owner filter missed the open PR and
    # ``gh pr create`` then failed with "already exists" on every run.
    pulls = [pr for page in request(f"repos/{gh_repo}/pulls?state=open&base=main&per_page=100")
             for pr in page if isinstance(pr, dict) and (pr.get("base") or {}).get("ref") == "main"
             and (pr.get("head") or {}).get("ref") == branch]
    if pulls:
        exact = [p for p in pulls if p["head"].get("sha") == cand.head]
        pr = max(exact or pulls, key=lambda p: p["number"])
        if pr["head"]["sha"] != cand.head:
            raise GateError(f"PR #{pr['number']} head is {pr['head']['sha'][:10]}, not the tested head {cand.head[:10]}")
        return pr
    created = gh("pr", "create", "-R", gh_repo, "--base", "main", "--head", branch,
                 "--title", f"Upstream sync {cand.head[:10]} (full suite green)",
                 "--body", f"Opened by scripts/ci/upstream_sync.py. The upstream sync {cand.head} passed the "
                           f"fork's full suite: {cand.url}\n\nThe gate merges this PR (merge commit, "
                           f"`--match-head-commit {cand.head}`, never `--admin`) once \"{REQUIRED_CHECK}\" is "
                           "green on exactly this head. Do not push to this branch.",
                 reason="upstream sync gate: open the landing PR")
    text = ((created.stdout or "") + (created.stderr or "")).strip()
    url = re.search(r"https://\S+/pull/(\d+)", text)
    if created.returncode or not url:
        if is_protection_refusal(text):
            raise Refused(f"GitHub refused the landing PR for {branch}: {text[-600:]}")
        raise GateError(f"gh pr create for {branch} failed: {text[-300:]}")
    return {"number": int(url[1]), "html_url": url[0], "head": {"sha": cand.head, "ref": branch}}


def required_check(gh_repo, sha, request) -> tuple[str, str]:
    """(green | pending | red, detail) of REQUIRED_CHECK on exactly ``sha``: what main's protection reads."""
    endpoint = (f"repos/{gh_repo}/commits/{sha}/check-runs?check_name={REQUIRED_CHECK.replace(' ', '%20')}"
                "&filter=all&per_page=100")
    runs = [run for page in request(endpoint) for run in page.get("check_runs", [])
            if run.get("name") == REQUIRED_CHECK and run.get("head_sha", sha) == sha
            and (run.get("app") or {}).get("id", ACTIONS_APP_ID) == ACTIONS_APP_ID]
    if not runs:
        return "pending", f'"{REQUIRED_CHECK}" (no check run on {sha[:10]} yet)'
    run = max(runs, key=lambda r: r["id"])  # a re-run supersedes the earlier attempt
    where = run.get("html_url") or run.get("details_url") or ""
    if run.get("status") != "completed":
        return "pending", f'"{REQUIRED_CHECK}" ({run.get("status")} on {sha[:10]}) {where}'.rstrip()
    if run.get("conclusion") == "success":
        return "green", where
    return "red", f'"{REQUIRED_CHECK}" concluded {run.get("conclusion")} on {sha[:10]} {where}'.rstrip()


def land(repo, cand: Candidate, *, gh_repo: str, request=github_pages, gh=gh_cli, protected=None) -> int:
    """Put the tested head on main through its PR, merged once the required check is green on exactly it."""
    main = _fetch_main(repo)
    if _parents_on_main(repo, cand.head, main):
        print(f"LANDED: sync {cand.head[:10]} is superseded; main {main} contains every parent")
        return LANDED
    if _is_ancestor(repo, cand.head, main):
        return LANDED
    if not _is_ancestor(repo, main, cand.head):
        if _adds_nothing(repo, cand.head, main):
            return LANDED  # main already holds everything it brings (a newer sync landed first)
        tree = _run(repo, "merge-tree", "--write-tree", "--no-messages", main, cand.head)
        if tree.returncode or not (tree.stdout or "").split("\n", 1)[0].strip():
            print(f"REFUSED: tested sync {cand.head[:10]} conflicts with main {main[:10]}; stage a new candidate")
            return REFUSED
    if protected is not None:
        protected.add(cand.head)
    pr = _land_pr(repo, cand, gh_repo, request, gh)
    where = f"PR #{pr['number']} {pr.get('html_url', '')}".rstrip()
    verdict, detail = required_check(gh_repo, cand.head, request)
    if verdict == "pending":
        print(f"NOT YET: {where} waits for {detail}; run again once it finishes")
        return NOT_YET
    if verdict == "red":
        print(f"REFUSED: {where} cannot land: {detail}")
        return REFUSED
    merged = gh("pr", "merge", str(pr["number"]), "-R", gh_repo, "--merge", "--match-head-commit", cand.head,
                "--subject", f"Merge tested upstream sync {cand.head[:10]} into main",
                "--body", f"Full suite: {cand.url}\n{REQUIRED_CHECK}: {detail}",
                reason="upstream sync gate: merge the landing PR")
    if merged.returncode:
        message = ((merged.stderr or "") + (merged.stdout or "")).strip()[-600:]
        if any(mark in message.lower() for mark in MERGE_RETRY_MARKS) and not is_protection_refusal(message):
            print(f"NOT YET: GitHub did not merge {where} yet ({message}); run again")
            return NOT_YET
        print(f"REFUSED: GitHub refused to merge {where}: {message}")
        return REFUSED
    main = _fetch_main(repo)
    if not _is_ancestor(repo, cand.head, main):
        print(f"NOT YET: {where} reported merged, but main {main[:10]} does not contain {cand.head[:10]} yet; run again")
        return NOT_YET
    # The run (and its verdict) outlives the branch; best effort, a leftover branch is harmless.
    _run(repo, "push", "-q", "origin", f":refs/heads/{BRANCH_PREFIX}{cand.head}", env=_gate_env("stage"))
    print(f"LANDED {cand.head} on main as {main} via {where}; full suite: {cand.url}")
    return LANDED


def _land_other_greens(repo, found, head, main, *, gh_repo, request, gh, protected=None) -> bool:
    landed = False
    for cand in found.values():
        if cand.status == "green" and cand.head != head and not _is_ancestor(repo, cand.head, main):
            landed = land(repo, cand, gh_repo=gh_repo, request=request, gh=gh, protected=protected) == LANDED or landed
    return landed


def _upstream_point(repo, sha) -> str | None:
    """The newest upstream/main commit ``sha`` contains; None when unknown (treated as a match)."""
    if _run(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
        _run(repo, "fetch", "-q", "origin", f"refs/heads/{BRANCH_PREFIX}{sha}")
    point = _run(repo, "merge-base", sha, UPSTREAM_REF)
    return point.stdout.strip() if point.returncode == 0 and point.stdout.strip() else None


def _stage_or_wait(repo, head, found, now) -> int:
    pending = [c for c in found.values() if c.status == "pending" and now - c.updated_at < STALE_PENDING_S]
    if pending:
        print(f"NOT YET: another sync candidate is in CI ({pending[0].url}); not staging {head[:10]}")
        return NOT_YET
    red = [c for c in found.values() if c.status == "red" and now - c.updated_at < RED_BACKOFF_S
           and _upstream_point(repo, c.head) in (None, _upstream_point(repo, head))]
    if red:
        print(f"NOT YET: a sync candidate with the same upstream failed its full suite within "
              f"{RED_BACKOFF_S // 3600} h ({red[0].url}); not staging {head[:10]} until upstream moves, "
              "the backoff ends, or the red candidate is rerun green")
        return NOT_YET
    _push(repo, head, f"refs/heads/{BRANCH_PREFIX}{head}", "stage")
    print(f"NOT YET: staged {head} as {BRANCH_PREFIX}{head}; it lands once its full suite is green")
    return NOT_YET


def _explain_red(repo, cand: Candidate, main: str, gh_repo: str, request, gh) -> None:
    """Which jobs/tests failed and whether main already fails them (upstream_sync_diagnostics). Evidence only:
    the verdict is already REFUSED, so a diagnostic fault is printed and never changes it."""
    try:
        if __package__:  # imported as scripts.ci.upstream_sync (tests): the same module object they patch
            from . import upstream_sync_diagnostics as diagnostics
        else:  # run as a script: scripts/ci is sys.path[0]
            import upstream_sync_diagnostics as diagnostics

        diagnostics.explain(repo, cand, main, gh_repo, request, gh, required=REQUIRED_JOBS, git=git)
    except Exception as exc:  # health: allow BLE001 -- evidence only: any fault is printed, REFUSED stands
        print(f"  diagnostics unavailable: {exc!r}")


def _close_superseded(repo, gh_repo, request, gh, protected=()) -> None:
    """Close open sync PRs a landed sync made redundant (upstream_sync_superseded). Never changes the verdict."""
    try:
        if __package__:  # imported as scripts.ci.upstream_sync (tests)
            from . import upstream_sync_superseded as superseded
        else:  # run as a script: scripts/ci is sys.path[0]
            import upstream_sync_superseded as superseded

        superseded.close_superseded(repo, gh_repo, request, gh, run=_run, env=_gate_env("stage"),
                                    prefix=BRANCH_PREFIX, protected=protected)
    except Exception as exc:  # health: allow BLE001 -- housekeeping only: any fault is printed, the verdict stands
        print(f"  superseded sync PRs: cleanup failed: {exc!r}")


def advance(repo: Path, head: str | None, *, gh_repo: str, request=github_pages, gh=gh_cli, now=None) -> int:
    """Land green candidates, then report ``head``'s state or stage it (module doc, exit codes).

    Every call then closes open sync PRs whose parents main already holds, including any this call's landing
    made redundant."""
    protected = set()
    verdict = _advance(repo, head, gh_repo=gh_repo, request=request, gh=gh, now=now, protected=protected)
    _close_superseded(repo, gh_repo, request, gh, protected)
    return verdict


def _advance(repo: Path, head: str | None, *, gh_repo: str, request, gh, now, protected) -> int:
    now = time.time() if now is None else now
    try:
        if head is not None:
            head = git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
        found = candidates(gh_repo, request)
        landed_other = _land_other_greens(repo, found, head, _fetch_main(repo), gh_repo=gh_repo, request=request,
                                         gh=gh, protected=protected)
        if head is None:
            return LANDED if landed_other else NOT_YET
        main = _fetch_main(repo)
        if _parents_on_main(repo, head, main):
            print(f"LANDED: sync {head[:10]} is superseded; main {main} contains every parent")
            return LANDED
        if _is_ancestor(repo, head, main) or _adds_nothing(repo, head, main):
            print(f"LANDED: main already has everything {head[:10]} brings")
            return LANDED
        cand = found.get(head)
        if cand is None:
            protected.add(head)
            return _stage_or_wait(repo, head, found, now)
        if cand.status == "green":
            return land(repo, cand, gh_repo=gh_repo, request=request, gh=gh, protected=protected)
        if cand.status == "red":
            print(f"REFUSED: the full suite failed on {head[:10]}: {cand.url}")
            _explain_red(repo, cand, main, gh_repo, request, gh)
            return REFUSED
        print(f"NOT YET: the full suite is still running on {head[:10]}: {cand.url}")
        return NOT_YET
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return REFUSED
    except (GateError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError,
            IndexError) as exc:
        print(f"REFUSED: upstream sync evidence/publication failed: {exc}")
        return REFUSED


def brings_upstream(repo, sha, base) -> bool:
    """``sha`` contains upstream/main commits that ``base`` (the fork's main) lacks."""
    upstream = _run(repo, "rev-parse", "-q", "--verify", f"{UPSTREAM_REF}^{{commit}}").stdout.strip()
    if not upstream:
        return False
    newest = _run(repo, "merge-base", sha, upstream).stdout.strip()
    return bool(newest) and not (base and _is_ancestor(repo, newest, base))


def guard_push(repo, sha) -> int:
    """Pre-push: refuse a push that brings upstream/main commits the fork's main lacks.

    Only this gate (stage/publish) may push such a commit; the pushed branch does not matter, as a
    branch carrying an untested sync reaches main by a PR merge just the same.
    """
    if os.environ.get(GATE_ENV) in ("publish", "stage"):
        return 0
    base = _run(repo, "rev-parse", "-q", "--verify", "refs/remotes/origin/main").stdout.strip()
    if not brings_upstream(repo, sha, base):
        return 0
    # A stale origin/main (a sync landed since the last fetch) must not refuse a branch built on it.
    if _run(repo, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main").returncode == 0:
        base = _run(repo, "rev-parse", "-q", "--verify", "refs/remotes/origin/main").stdout.strip()
        if not brings_upstream(repo, sha, base):
            return 0
    print(f"upstream sync guard: {sha[:10]} brings upstream/main commits that the fork's main lacks.\n"
          "  An upstream sync lands only after a full suite on the fork's own runners. Run instead:\n"
          f"    python3 scripts/ci/upstream_sync.py --head {sha[:12]}", file=sys.stderr)
    return 1


def origin_slug(repo) -> str:
    # The configured URL, not `remote get-url`'s insteadOf-rewritten one: it names the GitHub repo.
    url = git(repo, "config", "--get", "remote.origin.url")
    match = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", url)
    if not match:
        raise GateError(f"the upstream sync gate needs a GitHub origin, not {url}")
    return match[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--head", help="candidate commit; omitted: only land staged candidates that went green")
    parser.add_argument("--guard-push", metavar="SHA", help="pre-push check (scripts/check --commit runs it)")
    args = parser.parse_args(argv)
    if args.guard_push:
        return guard_push(args.repo, args.guard_push)
    try:
        slug = origin_slug(args.repo)
    except GateError as exc:
        print(f"REFUSED: {exc}")
        return REFUSED
    return advance(args.repo, args.head, gh_repo=slug)


if __name__ == "__main__":
    raise SystemExit(main())
