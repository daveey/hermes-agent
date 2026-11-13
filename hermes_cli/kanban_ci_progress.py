"""A CI wait must name a run that can start. [daveey]

On 2026-11-11 a hub card parked (``awaiting`` kind=ci) on GitHub Actions run 42993449998, whose jobs asked for
``self-hosted,ci-fast`` runners that no longer took jobs after the repo moved. ``_check_run`` only checked that the
ref looked like a run id, so the card slept until David asked why nothing was running; the run was cancelled
unstarted 16 minutes later. The shared "runners offline" blocker had been cleared from a one-time count that
morning, and nothing raised it again.

:func:`ci_run_progress` reads a GitHub Actions run's jobs and reports ``not_starting`` when one has been queued
longer than ``QUEUED_LIMIT_SECONDS`` with no runner assigned.

A job nobody picked up can also mean the runners are online but saturated. When the repo's or org's self-hosted
runner list is readable (``actions/runners``) and a runner that is ``online`` and ``busy`` carries every label the
job asks for, the run is ``busy`` instead: a park on it is allowed and nothing is woken, for up to
``BUSY_LIMIT_SECONDS`` of queueing. Past that cap the run is ``not_starting`` anyway: the org list can include
runners in groups this repo may not use, and such a runner busy with other repos' jobs would otherwise suppress the
wake forever. When every matching online runner is idle (``busy`` false) the job should have been taken long ago (a
stuck runner, or one in a group this repo cannot use), so the run is ``not_starting`` and the message names the idle
runner; one busy runner among idle ones reads as saturated, since a runner between two jobs is briefly idle. The repo
list is read first and decides alone when it has an online match. The lists count as unreadable when either read
fails (listing runners needs admin scope; a repo served only by org runners and a token without org admin show a
readable but empty repo list) and no online runner matched. A self-hosted job is then still ``busy`` when a job
asking for the same labels is running or a runner took it within ``QUEUED_LIMIT_SECONDS`` (this run's jobs, then
every in-progress run and the recently completed runs of the repo): 2026-11-13, run 43423320884 waited 15 min behind
~10 min jobs on the one-runner ``hermes-state`` pool, which was online and busy, and the card owning an approved PR
was woken behind ``runners-offline.hermes-state``. With no such job the run stays ``not_starting`` and every message
says only that no runner picked it up; a message says no online runner has the labels only when both lists were read.

* Park time (:func:`confirm`, called by ``kanban_awaiting.confirm_remote`` before the write txn): a ``kind=ci``
  park on such a run is refused with ``ci_run_unstartable`` naming the labels.
* While parked (:func:`sweep`, from the dispatcher's wake-check tick): each card whose latest park waits on a
  GitHub run is re-read at most every ``RECHECK_SECONDS``. On ``not_starting`` the card is woken with a comment and
  the shared blocker ``runners-offline.<labels>`` is asserted on it, so further cards that hit the same labels
  queue behind one probe instead of each sleeping. The first card woken for a blocker is its probe and may run at
  once (``kanban_shared_blockers.PROBE_RELEASED_EVENT``); a card woken while others already wait behind it is held
  with them, and its comment says for how long.
* Cost: :func:`submit_sweep` does one indexed query on the dispatcher thread and starts a sweep at most every
  ``SWEEP_EVERY_SECONDS`` per board; the card scan runs in the sweep, on the wake-check pool, and counts against
  that pool's ``MAX_CONCURRENT`` budget. One sweep reads at most ``MAX_CARDS_PER_SWEEP`` runs (least recently
  checked first) and starts no ``gh`` call after ``SWEEP_BUDGET_SECONDS``, so it ends at most
  ``FETCH_TIMEOUT_SECONDS`` past it. A run whose runner list the budget cut off is read again first next sweep.

A ref is checked when it is (or contains) ``<owner>/<repo>/actions/runs/<id>``, or is a bare run id whose wake
check names the repo (``gh run view <id> -R owner/repo``). A ref naming ``<owner>/<repo>/pull/<n>`` (optionally with
its head sha) is read by :func:`pr_progress`: 2026-11-13, state PR #2495 conflicted with its base, so GitHub started
no ``pull_request`` workflow for the pushed head, and the card parked on its checks would have slept until its cap.
A conflicting PR is ``not_starting`` (a park refused; a parked card woken), and so is a head with no workflow run
``QUEUED_LIMIT_SECONDS`` after the later of the park and the PR's last update. Neither asserts a
``runners-offline`` blocker: they are one PR's, not a runner pool's. The head's queued runs go through
:func:`ci_run_progress`, so a PR wait gets the runner checks too. Anything else (a release, an unknown repo) is
``untracked``. A GitHub error is logged and fails open: the park is allowed and the next sweep tries again.

GitHub is read through the home's ``kanban.pr_acceptance.gh_command_prefix`` (the credential broker), as the
dispatcher's PR-state read does (:mod:`hermes_cli.kanban_gh_argv`): the hub's ``gh`` has no login, and a bare read
failed open on every run, so the check never fired there (agent-c2b8fc1abf). A fail-open for want of a credential is
logged as one WARNING per ``NO_CREDENTIAL_WARN_SECONDS`` per home and board, so a disabled check is visible, not
silent; a missing ``gh`` binary gets its own wording (install gh), since no prefix or login fixes it. The sweep runs
on a pool thread with the dispatching home rebound, so each board reads its own home's prefix.
"""
from __future__ import annotations

import calendar
import contextvars
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CI_RUN_UNSTARTABLE = "ci_run_unstartable"
NOT_STARTING = "not_starting"
BUSY = "busy"
QUEUED_LIMIT_SECONDS = 15 * 60
BUSY_LIMIT_SECONDS = 8 * QUEUED_LIMIT_SECONDS  # how long busy runners may hold back a wake
RECHECK_SECONDS = 5 * 60
FETCH_TIMEOUT_SECONDS = 20
NO_CREDENTIAL_WARN_SECONDS = 60 * 60
SWEEP_EVERY_SECONDS = 60
MAX_CARDS_PER_SWEEP = 6
SWEEP_BUDGET_SECONDS = 90
RECENT_RUNS_MAX = 4  # completed runs whose jobs the unreadable-runner-list fallback reads
IN_PROGRESS_RUNS_MAX = 40  # in-progress runs whose jobs it reads (all of them on a normal day: 23 on 2026-11-13)
POOL_SCAN_READERS = 6  # those job lists read at once: each read is a broker vend + gh call, ~3 s on the hub
POOL_SCAN_TTL_SECONDS = SWEEP_EVERY_SECONDS  # how long one repo-wide scan answers for the same repo and labels
BLOCKER_PREFIX = "runners-offline"
AUTHOR = "kanban-dispatcher"

_SLUG = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_RUN_PATH = re.compile(rf"(?:https?://github\.com/)?({_SLUG})/actions/runs/(\d+)")
_BARE_RUN = re.compile(r"\d{6,}")
_PR_PATH = re.compile(rf"(?:https?://github\.com/)?({_SLUG})/pull/(\d+)")
_AT_SHA = re.compile(r"@([0-9a-fA-F]{7,40})(?![0-9a-fA-F])")
_FULL_SHA = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])")
PR_QUEUED_RUNS_MAX = 3  # queued runs of a PR head handed to ci_run_progress per read
_REPO_FLAG = re.compile(rf"(?:^|\s)(?:-R|--repo)(?:=|\s+)['\"]?({_SLUG})")
_GENERIC_LABELS = frozenset({"self-hosted", "linux", "macos", "windows", "x64", "arm64"})

Fetch = Callable[[str, str], dict]
FetchRunners = Callable[[str], Optional[list]]


class NoCredential(RuntimeError):
    """gh has no usable login here (exit 4 / HTTP 401), or the broker prefix is unusable."""


class GhMissing(RuntimeError):
    """No ``gh`` executable: the fix is installing it, not a credential."""


class OverBudget(RuntimeError):
    """A sweep's ``SWEEP_BUDGET_SECONDS`` ran out before the next ``gh`` call; the run is read again next sweep."""


@dataclass
class Progress:
    state: str  # "untracked" | "unknown" | "ok" | "busy" | "not_starting"
    repo: str = ""
    run_id: str = ""
    labels: list[str] = field(default_factory=list)
    queued_seconds: int = 0
    detail: str = ""
    no_credential: bool = False
    gh_missing: bool = False
    deferred: bool = False  # unknown: the sweep budget ran out before the runner list was read
    runners_readable: bool = False  # not_starting: both runner lists were read (or one had an online match)
    idle_runner: str = ""  # not_starting: an online runner for the labels is idle, yet took no job
    busy_runner: str = ""  # not_starting: an online runner for the labels is busy, past BUSY_LIMIT_SECONDS
    pr: int = 0  # the pull request a PR-ref wait names
    per_pr: bool = False  # not_starting for this PR alone (a conflict, no run created): no runner blocker
    remedy: str = ""  # per_pr: what the owner does about it
    settling: bool = False  # unknown only because GitHub has not computed ``mergeable`` yet: no warning


def pr_target(ref: str, hint: str = "") -> Optional[tuple[str, int, Optional[str]]]:
    """``(owner/repo, PR number, head sha or None)`` for a ref naming ``<owner>/<repo>/pull/<n>``, else None. The
    sha is ``@<7-40 hex>`` in the ref, else the one 40-hex sha the ref holds, else the one 40-hex sha in ``hint``
    (the park's wake check and reason). Several distinct shas there give None, so the PR's current head is read:
    a guess could pick an old head or a base sha, which never matches the head, and every sweep would then read
    the PR as "head moved" (``ok``) and miss its conflict or missing run. A ref that names a run is a run
    (:func:`run_target`), not a PR."""
    ref = (ref or "").strip()
    m = _PR_PATH.search(ref)
    if m is None or run_target(ref, hint) is not None:
        return None
    at = _AT_SHA.search(ref)
    return m.group(1), int(m.group(2)), at.group(1).lower() if at else _only_sha(ref, hint or "")


def _only_sha(ref: str, hint: str) -> Optional[str]:
    """The 40-hex sha a PR ref without ``@<sha>`` names: the ref's own when it holds exactly one, else the hint's
    when the ref holds none and the hint exactly one (case-insensitive, repeats counted once), else None."""
    for text in (ref, hint):
        found = {s.lower() for s in _FULL_SHA.findall(text)}
        if found:
            return found.pop() if len(found) == 1 else None
    return None


def run_target(ref: str, hint: str = "") -> Optional[tuple[str, str]]:
    """``(owner/repo, run id)`` for a GitHub Actions run ref, else None. ``hint`` (the park's wake check) supplies
    the repo for a bare run id."""
    ref = (ref or "").strip()
    m = _RUN_PATH.search(ref)
    if m:
        return m.group(1), m.group(2)
    if _BARE_RUN.fullmatch(ref):
        repo = _REPO_FLAG.search(hint or "")
        if repo:
            return repo.group(1), ref
    return None


def _gh_api(path: str) -> dict:
    """``gh api <path>`` as JSON through the home's broker prefix + ``gh``. Raises on any failure:
    :class:`GhMissing` with no gh; :class:`NoCredential` when gh has no usable login or the prefix is unusable
    (config text never included)."""
    from hermes_cli.kanban_gh_argv import GhArgvUnavailable, GhNotInstalled, dispatcher_gh_argv
    try:
        gh = dispatcher_gh_argv()
    except GhNotInstalled as exc:
        raise GhMissing(str(exc)) from None
    except GhArgvUnavailable as exc:
        raise NoCredential(f"gh unavailable: {exc}") from None
    p = subprocess.run([*gh, "api", path],
                       capture_output=True, text=True, timeout=FETCH_TIMEOUT_SECONDS, check=False,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        detail = f"gh api exit {p.returncode}: {err[-200:]}"
        # 4 = gh's "authentication required"; HTTP 401 = a token GitHub refuses.
        if p.returncode == 4 or "HTTP 401" in err:
            raise NoCredential(detail)
        raise RuntimeError(detail)
    return json.loads(p.stdout or "{}")


def _fetch_jobs(repo: str, run_id: str) -> dict:
    """The run's jobs from the GitHub API through ``gh``. Raises on any failure."""
    return _gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")


def _fetch_runners(repo: str, labels: list[str], *, deadline: Optional[float] = None) -> Optional[list]:
    """Self-hosted runners the repo can use (repo-level, then its owner's org-level). When the repo list already has
    an online runner for ``labels`` the org list is not read. None when a list was unreadable and the lists read
    hold no online runner for ``labels``: listing runners needs admin scope, which a worker's token often lacks, and
    a repo served only by org runners then shows a readable but empty repo list, which says nothing about the
    runners. The org list may hold runners in groups this repo cannot use (a false ``busy``, capped by
    ``BUSY_LIMIT_SECONDS``). Raises :class:`OverBudget` when ``deadline`` (monotonic) passed before a read."""
    found: list = []
    unread = False
    for path in (f"repos/{repo}/actions/runners?per_page=100",
                 f"orgs/{repo.split('/', 1)[0]}/actions/runners?per_page=100"):
        if _online_runner(found, labels) is not None:
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise OverBudget(f"sweep budget spent before {path.split('?', 1)[0]} was read")
        try:
            data = _gh_api(path)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            logger.debug("ci run progress: %s unreadable: %s", path, exc)
            unread = True
            continue
        found += list((data or {}).get("runners") or [])
    if unread and _online_runner(found, labels) is None:
        return None
    return found


def _online_runners(runners: list, labels: list[str]) -> list[dict]:
    """The ``online`` runners carrying every label in ``labels`` (case-insensitive)."""
    want = {x.lower() for x in labels}
    found = []
    for runner in runners:
        if not isinstance(runner, dict) or runner.get("status") != "online":
            continue
        have = {str(x.get("name") or "").lower() for x in runner.get("labels") or [] if isinstance(x, dict)}
        if want <= have:
            found.append(runner)
    return found


def _name(runner: dict) -> str:
    return str(runner.get("name") or "(unnamed)")


def _online_runner(runners: list, labels: list[str]) -> Optional[str]:
    """Name of an ``online`` runner carrying every label in ``labels`` (case-insensitive), else None."""
    found = _online_runners(runners, labels)
    return _name(found[0]) if found else None


def _busy_runner(runners: list, labels: list[str]) -> Optional[str]:
    """Name of an online runner for ``labels`` that is running a job, else None. Only an explicit ``busy: false``
    says a runner is free: one without the flag counts as busy (saturated, the pre-flag reading)."""
    return next((_name(r) for r in _online_runners(runners, labels) if r.get("busy") is not False), None)


def _epoch(value) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def ci_run_progress(ref: str, *, hint: str = "", fetch: Optional[Fetch] = None,
                    fetch_runners: Optional[FetchRunners] = None, now: Optional[float] = None,
                    deadline: Optional[float] = None) -> Progress:
    """Whether a CI run can still start. Never raises: an unreadable run is ``unknown``; a stuck job whose labels
    an online runner carries is ``busy`` (saturated runners, not missing ones). A stuck run costs up to three
    ``gh`` calls (jobs, repo and org runner lists), each bounded by ``FETCH_TIMEOUT_SECONDS``. With ``deadline``
    (monotonic) no runner-list call starts after it: the run is ``unknown`` with ``deferred`` set."""
    target = run_target(ref, hint)
    if target is None:
        return Progress("untracked")
    repo, run_id = target
    try:
        data = (fetch or _fetch_jobs)(repo, run_id)
    except GhMissing as exc:
        return Progress("unknown", repo, run_id, detail=str(exc)[-300:], gh_missing=True)
    except NoCredential as exc:
        return Progress("unknown", repo, run_id, detail=str(exc)[-300:], no_credential=True)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return Progress("unknown", repo, run_id, detail=str(exc)[-300:])
    now = time.time() if now is None else now
    stuck: list[tuple[int, list[str]]] = []
    for job in (data or {}).get("jobs") or []:
        if not isinstance(job, dict) or job.get("status") != "queued" or job.get("runner_name"):
            continue  # started, done, waiting on an environment approval, or a runner already took it
        created = _epoch(job.get("created_at"))
        if created is None:
            continue
        age = int(now - created)
        if age > QUEUED_LIMIT_SECONDS:
            stuck.append((age, [str(x) for x in job.get("labels") or []]))
    if not stuck:
        return Progress("ok", repo, run_id)
    age, labels = max(stuck)
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise OverBudget("sweep budget spent before the runner list was read")
        runners = fetch_runners(repo) if fetch_runners else _fetch_runners(repo, labels, deadline=deadline)
    except OverBudget as exc:
        return Progress("unknown", repo, run_id, labels, age, detail=str(exc), deferred=True)
    if runners is not None:
        busy = _busy_runner(runners, labels)
        if busy is not None and age <= BUSY_LIMIT_SECONDS:
            logger.info("ci run %s/%s queued %ss: runner %s is online and busy for %s, so the runners are saturated",
                        repo, run_id, age, busy, ",".join(labels))
            return Progress(BUSY, repo, run_id, labels, age,
                            detail=f"runner {busy} is online and busy for those labels; the runners are saturated")
        if busy is not None:
            return Progress(NOT_STARTING, repo, run_id, labels, age, runners_readable=True, busy_runner=busy)
        idle = _online_runner(runners, labels)
        if idle is not None:
            return Progress(NOT_STARTING, repo, run_id, labels, age, runners_readable=True, idle_runner=idle)
    elif "self-hosted" in {x.lower() for x in labels}:
        try:
            took = _recent_job_on(repo, labels, (data or {}).get("jobs") or [], now, deadline, run_id=run_id)
        except OverBudget as exc:
            return Progress("unknown", repo, run_id, labels, age, detail=str(exc), deferred=True)
        if took:
            logger.info("ci run %s/%s queued %ss: runner list unreadable, but %s on %s, so the runners are "
                        "saturated", repo, run_id, age, took, ",".join(labels))
            return Progress(BUSY, repo, run_id, labels, age,
                            detail=f"runner list unreadable, but {took} on those labels; the runners are saturated")
    return Progress(NOT_STARTING, repo, run_id, labels, age, runners_readable=runners is not None)


def _recent_job_on(repo: str, labels: list[str], jobs: list, now: float, deadline: Optional[float], *,
                   run_id: str = "") -> str:
    """With the runner lists unreadable: a job asking for exactly ``labels`` that a runner is running, or took
    within ``QUEUED_LIMIT_SECONDS``, shows the pool alive. This run's jobs first, then the jobs of every
    in-progress run of the repo (up to ``IN_PROGRESS_RUNS_MAX``), then of up to ``RECENT_RUNS_MAX`` recently
    completed runs. "" when none or unreadable; raises :class:`OverBudget` when ``deadline`` passed before a read.

    GitHub has no jobs-by-label query, and the run list's order says nothing about which run holds the pool's
    runner: on 2026-11-13 the run holding the one ``hermes-state`` runner was 21st of 23 in-progress runs (and of
    another workflow than the queued run), so a 4-run window answered ``not_starting`` for a busy pool. Every
    in-progress run is read instead (``POOL_SCAN_READERS`` at a time), stopping at the first batch with a match. The
    answer for a repo and label set is kept ``POOL_SCAN_TTL_SECONDS``, so cards waiting on the same pool in one sweep
    share one scan."""
    want = {x.lower() for x in labels}
    cutoff = now - QUEUED_LIMIT_SECONDS

    def took(js) -> str:
        for j in js:
            if not isinstance(j, dict) or not j.get("runner_name"):
                continue
            if {str(x).lower() for x in j.get("labels") or []} != want:
                continue
            started = _epoch(j.get("started_at"))
            if j.get("status") == "in_progress" or (started is not None and started >= cutoff):
                return f"runner {j.get('runner_name')} took job {j.get('id')}"
        return ""

    def read(path: str) -> dict:
        if deadline is not None and time.monotonic() >= deadline:
            raise OverBudget(f"sweep budget spent before {path.split('?', 1)[0]} was read")
        return _gh_api(path) or {}

    unreadable = (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired)
    errors: list[BaseException] = []  # failed reads, so the scan goes on but an answer with one is not remembered

    def batch(paths: list[str]) -> str:
        """Jobs of several runs at once (each read is a broker vend plus a GitHub call, ~3 s): the first match in
        run order. A failed read is noted in ``errors`` and the rest of the scan goes on, since a later batch
        can still hold the pool's runner."""
        if deadline is not None and time.monotonic() >= deadline:
            raise OverBudget(f"sweep budget spent before {paths[0].split('?', 1)[0]} was read")
        # Each read runs in a copy of this thread's context: a fresh pool thread would lose the bound home, and
        # ``dispatcher_gh_argv`` would read the launch home's gh_command_prefix (another home's credential).
        with ThreadPoolExecutor(max_workers=len(paths), thread_name_prefix="ci-pool-scan") as ex:
            futures = [ex.submit(contextvars.copy_context().run, _gh_api, p) for p in paths]
        for fut in futures:
            exc = fut.exception()
            if exc is not None:
                if not isinstance(exc, unreadable):
                    raise exc
                errors.append(exc)
            elif hit := took((fut.result() or {}).get("jobs") or []):
                return hit
        return ""

    found = took(jobs)
    if found:
        return found
    from hermes_constants import get_hermes_home
    key = (str(get_hermes_home()), repo.lower(), frozenset(want))  # by home too: its credential did the reads
    seen = _POOL_SCANS.get(key)
    if seen is not None and time.monotonic() - seen[0] < POOL_SCAN_TTL_SECONDS:
        return seen[1]
    lists = (("in_progress", 100, IN_PROGRESS_RUNS_MAX), ("completed", 10, RECENT_RUNS_MAX))
    for status, per_page, limit in lists:
        if found:
            break
        try:
            listed = read(f"repos/{repo}/actions/runs?status={status}&per_page={per_page}").get("workflow_runs")
        except OverBudget:
            raise
        except unreadable as exc:
            errors.append(exc)  # the other list may still show the pool alive
            continue
        runs = [r for r in listed or [] if isinstance(r, dict) and r.get("id") and str(r["id"]) != str(run_id)
                and (status == "in_progress" or (_epoch(r.get("updated_at")) or 0) >= cutoff)][:limit]
        paths = [f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100" for run in runs]
        for i in range(0, len(paths), POOL_SCAN_READERS):
            found = batch(paths[i:i + POOL_SCAN_READERS])
            if found:
                break
    if not found and errors:
        logger.debug("ci run progress: recent runs of %s unreadable: %s", repo, errors[0])
        return ""  # an unread list is no evidence either way: not remembered
    _POOL_SCANS[key] = (time.monotonic(), found)
    return found


def pr_progress(repo: str, number: int, sha: Optional[str], parked_at: Optional[float], *,
                fetch: Optional[Fetch] = None, fetch_runners: Optional[FetchRunners] = None,
                now: Optional[float] = None, deadline: Optional[float] = None) -> Progress:
    """Whether a pull request's checks can still start for the head a wait names. Never raises. A closed or merged
    PR, or a head that moved off ``sha``, is ``ok``: the card's own probe answers those. A PR that conflicts with
    its base is ``not_starting`` (GitHub starts no ``pull_request`` workflow for it), and so is a head with no
    workflow run ``QUEUED_LIMIT_SECONDS`` after the later of ``parked_at`` (None: now) and the PR's last update;
    both are ``per_pr``. Queued runs of the head go through :func:`ci_run_progress`. ``mergeable`` null with runs
    is ``unknown`` (``settling``): GitHub computes it lazily, so the next sweep reads it again."""
    now = time.time() if now is None else now
    try:
        pr = _gh_api(f"repos/{repo}/pulls/{number}") or {}
        if pr.get("state") != "open" or pr.get("merged"):
            return Progress("ok", repo, pr=number)
        head = str((pr.get("head") or {}).get("sha") or "").lower()
        if sha and not head.startswith(sha.lower()):
            return Progress("ok", repo, pr=number)
        base = str((pr.get("base") or {}).get("ref") or "its base")
        mergeable = pr.get("mergeable")
        if mergeable is False or pr.get("mergeable_state") == "dirty":
            return Progress(NOT_STARTING, repo, pr=number, per_pr=True, detail=(
                f"PR #{number} conflicts with {base}: pull_request checks cannot start until it is rebased"),
                remedy=(f"Rebase it onto {base} (or merge {base} into it), push, and park on the new head's CI; "
                        f"a park on PR #{number} while it conflicts is refused ({CI_RUN_UNSTARTABLE})."))
        if deadline is not None and time.monotonic() >= deadline:
            raise OverBudget("sweep budget spent before the head's runs were read")
        runs = (_gh_api(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100") or {}).get("workflow_runs")
    except GhMissing as exc:
        return Progress("unknown", repo, pr=number, detail=str(exc)[-300:], gh_missing=True)
    except NoCredential as exc:
        return Progress("unknown", repo, pr=number, detail=str(exc)[-300:], no_credential=True)
    except OverBudget as exc:
        return Progress("unknown", repo, pr=number, detail=str(exc), deferred=True)
    except (OSError, RuntimeError, ValueError, AttributeError, subprocess.TimeoutExpired) as exc:
        return Progress("unknown", repo, pr=number, detail=str(exc)[-300:])
    runs = [r for r in runs or [] if isinstance(r, dict) and r.get("id")]
    if not runs:
        since = max(parked_at if parked_at is not None else now, _epoch(pr.get("updated_at")) or 0)
        if now - since >= QUEUED_LIMIT_SECONDS:
            return Progress(NOT_STARTING, repo, pr=number, per_pr=True, detail=(
                f"no CI run was created for {head[:10]} in {QUEUED_LIMIT_SECONDS // 60} min "
                f"(PR #{number} mergeable={mergeable})"),
                remedy=("Check whether the PR conflicts with its base (rebase it if so), or push a commit that "
                        "triggers CI, then park on the new head's CI."))
        return Progress("ok" if mergeable is not None else "unknown", repo, pr=number, settling=True)
    for run in [r for r in runs if r.get("status") in ("queued", "waiting", "requested", "pending")][
            :PR_QUEUED_RUNS_MAX]:
        p = ci_run_progress(f"{repo}/actions/runs/{run['id']}", fetch=fetch, fetch_runners=fetch_runners, now=now,
                            deadline=deadline)
        if p.state != "ok":
            p.pr = number
            return p
    if mergeable is None:
        return Progress("unknown", repo, pr=number, settling=True, detail="GitHub has not computed mergeable yet")
    return Progress("ok", repo, pr=number)


def progress(ref: str, *, hint: str = "", parked_at: Optional[float] = None, fetch: Optional[Fetch] = None,
             fetch_runners: Optional[FetchRunners] = None, deadline: Optional[float] = None) -> Progress:
    """:func:`ci_run_progress` for a run ref, :func:`pr_progress` for a PR ref, else ``untracked``."""
    if run_target(ref, hint) is not None:
        return ci_run_progress(ref, hint=hint, fetch=fetch, fetch_runners=fetch_runners, deadline=deadline)
    target = pr_target(ref, hint)
    if target is None:
        return Progress("untracked")
    return pr_progress(*target, parked_at, fetch=fetch, fetch_runners=fetch_runners, deadline=deadline)


def describe(p: Progress) -> str:
    if p.per_pr:
        return p.detail
    labels = ",".join(p.labels) or "(none)"
    if p.idle_runner:
        seen = (f"runner {p.idle_runner} is online and idle for them yet took no job: a stuck runner, or one in a "
                "runner group this repo cannot use")
    elif p.busy_runner:
        seen = (f"runner {p.busy_runner} is online and busy for them, but the job waited past the "
                f"{BUSY_LIMIT_SECONDS // 60} min busy cap: that runner may be in a group this repo cannot use")
    elif p.runners_readable:
        seen = "no online runner has them"
    else:
        seen = "runner list unreadable, so they may be offline or saturated"
    return (f"CI run {p.run_id} queued {p.queued_seconds // 60} min: no runner picked it up in "
            f"{QUEUED_LIMIT_SECONDS // 60} min (labels {labels}, {p.repo}; {seen}), so it may never start")


def blocker_id(labels: list[str]) -> str:
    """``runners-offline.<labels>``: one shared blocker per runner pool (generic labels dropped)."""
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id
    specific = [x.lower() for x in labels if x.lower() not in _GENERIC_LABELS] or [x.lower() for x in labels]
    return normalize_blocker_id(".".join([BLOCKER_PREFIX, *specific])) or BLOCKER_PREFIX


def confirm(awaiting: Optional[dict], *, hint: str = "", fetch: Optional[Fetch] = None,
            fetch_runners: Optional[FetchRunners] = None) -> None:
    """Refuse a ``kind=ci`` park on a run that is not starting. Network: call before the park's write txn. A run
    with a job queued past ``QUEUED_LIMIT_SECONDS`` makes up to three ``gh`` calls here (jobs, then the repo and org
    runner lists), each up to ``FETCH_TIMEOUT_SECONDS``, so such a park can take about a minute when GitHub hangs;
    a started or freshly queued run makes one."""
    if not awaiting or awaiting.get("kind") != "ci":
        return
    p = progress(str(awaiting.get("ref") or ""), hint=hint, fetch=fetch, fetch_runners=fetch_runners)
    if p.state == "unknown":
        if not p.settling:
            logger.warning("ci run progress for %s/%s unreadable, allowing the park: %s", p.repo,
                           p.run_id or f"pull/{p.pr}", p.detail)
        return
    if p.state != NOT_STARTING:
        return
    from hermes_cli.kanban_awaiting import AwaitingError
    if p.per_pr:
        raise AwaitingError(CI_RUN_UNSTARTABLE, f"{p.detail}. A wait on its checks would sleep until someone "
                                                f"notices. {p.remedy}")
    raise AwaitingError(CI_RUN_UNSTARTABLE, (
        f"{describe(p)}. A wait on it would sleep until someone notices. Get a runner with labels "
        f"{','.join(p.labels)} taking jobs, or re-run the job on runners that exist (fix the workflow's runs-on), or "
        f"park behind the shared blocker: kanban_block(kind='time', blocker_id='{blocker_id(p.labels)}', ...)."))


# --- while parked --------------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_LAST_CHECK: dict[tuple[str, str], float] = {}   # (db, task id) -> monotonic time of its last read; live waits only
_LAST_SWEEP: dict[str, float] = {}               # db -> monotonic time its last sweep started
_SWEEPS: dict[str, Future] = {}
# (home, repo, labels) -> (monotonic time, answer) of the last repo-wide scan for a job on those labels
_POOL_SCANS: dict[tuple[str, str, frozenset], tuple[float, str]] = {}
# (home, board db) -> monotonic time of the last disabled-check WARNING. Keyed like ``_LAST_CHECK`` (and by home,
# whose gh_command_prefix the read used) so one home's warning never hides another's in a multi-home process.
_WARNED_AT: dict[tuple[str, str], float] = {}


def _warn_disabled(p: Progress, key: tuple[str, str]) -> None:
    """One WARNING per ``NO_CREDENTIAL_WARN_SECONDS`` per ``key``: with no gh or no credential every read fails open."""
    with _LOCK:
        now = time.monotonic()
        last = _WARNED_AT.get(key)
        if last is not None and now - last < NO_CREDENTIAL_WARN_SECONDS:
            return
        _WARNED_AT[key] = now
    disabled = "CI-run progress check disabled (cards parked on CI runs are neither refused nor woken)"
    if p.gh_missing:
        logger.warning("kanban ci run progress: %s; gh not installed: %s. Install the GitHub CLI (gh) on PATH, "
                       "~/bin or ~/.local/bin of this host.", p.detail, disabled)
    else:
        logger.warning("kanban ci run progress: %s; no credential: %s. Set kanban.pr_acceptance.gh_command_prefix "
                       "to the credential broker or log gh in.", p.detail, disabled)


def _ci_waits(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """``(task id, ref, hint)`` of every scheduled card whose latest park waits on a trackable CI run."""
    from hermes_cli.kanban_awaiting import _latest_park

    found = []
    for (task_id,) in conn.execute("SELECT id FROM tasks WHERE status = 'scheduled'").fetchall():
        awaiting, reason = _latest_park(conn, task_id)
        if not isinstance(awaiting, dict) or awaiting.get("kind") != "ci":
            continue
        row = conn.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ?", (task_id,)).fetchone()
        hint = " ".join(x for x in (row[0] if row else "", reason) if x)
        ref = str(awaiting.get("ref") or "")
        if run_target(ref, hint) or pr_target(ref, hint):
            found.append((task_id, ref, hint))
    return found


def _parked_at(conn: sqlite3.Connection, task_id: str) -> Optional[float]:
    """When the card's latest park was written (epoch seconds), else None."""
    row = conn.execute("SELECT created_at FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued') "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _others_behind(conn: sqlite3.Connection, task_id: str, bid: str) -> list[str]:
    """Other live cards already waiting behind the open shared blocker ``bid`` that ``shared_blocker_guard``
    counts (a provider-scoped blocker ignores cards routed to another provider)."""
    from hermes_cli import kanban_shared_blockers as ksb
    st = ksb.blocker_states(conn).get(bid)
    if st is None or not st.is_open:
        return []
    return sorted(t for t in st.behind if t != task_id and ksb.task_applies(conn, st, t))


def apply(conn: sqlite3.Connection, task_id: str, ref: str, p: Progress) -> Optional[str]:
    """Wake a card parked on a run that is not starting, comment why, and assert the shared blocker on it. The
    first card behind the blocker is released as its probe and may run at once; a later one is held with the
    others (``shared_blocker_guard``), and the comment says so."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_shared_blockers as ksb
    from hermes_cli.kanban_awaiting import _latest_park
    from hermes_cli.kanban_wake_check import _wake

    if p.per_pr:
        return _apply_per_pr(conn, task_id, ref, p)
    bid = blocker_id(p.labels)
    with kb.write_txn(conn):
        awaiting, _reason = _latest_park(conn, task_id)
        if not isinstance(awaiting, dict) or awaiting.get("kind") != "ci" or str(awaiting.get("ref") or "") != ref:
            return None  # re-parked on something else since the read
        others = _others_behind(conn, task_id, bid)
        if others:
            minutes = max(1, ksb.probe_interval_seconds() // 60)
            hold = (f"{len(others)} other card(s) ({', '.join(others[:3])}) already wait behind shared blocker "
                    f"{bid}, so this card is held with them: one card probes it at a time, at most every {minutes} "
                    "min, and every held card is woken when it clears.")
        else:
            hold = (f"This card is the probe for shared blocker {bid} and can run now; other cards that hit it "
                    "wait behind it.")
        note = (f"Woken: {describe(p)}. Re-parking on this run will be refused ({CI_RUN_UNSTARTABLE}). Get a runner "
                f"for those labels taking jobs or re-run on runners that exist. {hold}")
        woke = _wake(conn, task_id, CI_RUN_UNSTARTABLE, {
            "ref": ref, "repo": p.repo, "run_id": p.run_id, "labels": p.labels, "queued_seconds": p.queued_seconds,
            "note": note})
        if not woke:
            return None
        kb.add_comment(conn, task_id, AUTHOR, note)
        run_id = kb._synthesize_ended_run(conn, task_id, outcome=CI_RUN_UNSTARTABLE, summary=describe(p),
                                          metadata={"blocker_id": bid, "ci_run": ref, "labels": p.labels})
        if not others:  # spent by the card's next run, not by runs before this one (_released_as_probe)
            kb._append_event(conn, task_id, ksb.PROBE_RELEASED_EVENT,
                             {"blocker_id": bid, "via": CI_RUN_UNSTARTABLE, "after_run": run_id})
    logger.info("kanban: woke %s, %s", task_id, describe(p))
    return woke


def _apply_per_pr(conn: sqlite3.Connection, task_id: str, ref: str, p: Progress) -> Optional[str]:
    """Wake a card whose PR's checks cannot start (a conflict, no run created) with one comment. No shared blocker:
    the cause is this PR's, not a runner pool's, so no other card queues behind it."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_awaiting import _latest_park
    from hermes_cli.kanban_wake_check import _wake

    with kb.write_txn(conn):
        awaiting, _reason = _latest_park(conn, task_id)
        if not isinstance(awaiting, dict) or awaiting.get("kind") != "ci" or str(awaiting.get("ref") or "") != ref:
            return None  # re-parked on something else since the read
        note = f"Woken: {p.detail}, so the wait on its checks would sleep until its cap. {p.remedy}"
        woke = _wake(conn, task_id, CI_RUN_UNSTARTABLE, {"ref": ref, "repo": p.repo, "pr": p.pr, "note": note})
        if not woke:
            return None
        kb.add_comment(conn, task_id, AUTHOR, note)
        kb._synthesize_ended_run(conn, task_id, outcome=CI_RUN_UNSTARTABLE, summary=p.detail,
                                 metadata={"ci_pr": ref})
    logger.info("kanban: woke %s, %s", task_id, p.detail)
    return woke


def sweep(conn: sqlite3.Connection, *, fetch: Optional[Fetch] = None,
          fetch_runners: Optional[FetchRunners] = None) -> list[str]:
    """Re-read CI-waiting cards' runs and wake the cards whose run is not starting: each card at most once per
    ``RECHECK_SECONDS``, at most ``MAX_CARDS_PER_SWEEP`` per sweep (least recently checked first), and no ``gh``
    call started after ``SWEEP_BUDGET_SECONDS`` (a run cut off before its runner list goes first next sweep).
    Network: run it off the dispatcher thread (``submit_sweep``). Never raises."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_constants import get_hermes_home

    woken: list[str] = []
    try:
        db = str(kbc._main_db_file(conn) or "")
        warn_key = (str(get_hermes_home()), db)
        deadline = time.monotonic() + SWEEP_BUDGET_SECONDS
        waits = _ci_waits(conn)
        live = {(db, task_id) for task_id, _ref, _hint in waits}
        now = time.monotonic()
        with _LOCK:
            for key in [k for k in _LAST_CHECK if k[0] == db and k not in live]:
                del _LAST_CHECK[key]  # re-parked on something else, woken or gone
            due = sorted((_LAST_CHECK.get((db, task_id), float("-inf")), task_id, ref, hint)
                         for task_id, ref, hint in waits
                         if now - _LAST_CHECK.get((db, task_id), float("-inf")) >= RECHECK_SECONDS)
        for checked, (_last, task_id, ref, hint) in enumerate(due):
            if checked >= MAX_CARDS_PER_SWEEP or time.monotonic() >= deadline:
                break  # the rest are the least recently checked ones next sweep
            with _LOCK:
                _LAST_CHECK[(db, task_id)] = time.monotonic()
            p = progress(ref, hint=hint, parked_at=_parked_at(conn, task_id), fetch=fetch,
                         fetch_runners=fetch_runners, deadline=deadline)
            if p.deferred:
                with _LOCK:
                    _LAST_CHECK.pop((db, task_id), None)  # least recently checked: first next sweep
                continue
            if p.no_credential or p.gh_missing:
                _warn_disabled(p, warn_key)
            elif p.state == "unknown" and not p.settling:
                logger.warning("ci run progress for %s (%s) unreadable: %s", task_id, ref, p.detail)
            if p.state == NOT_STARTING and apply(conn, task_id, ref, p):
                woken.append(task_id)
    except Exception:
        logger.warning("kanban ci run progress sweep failed", exc_info=True)
    return woken


def _sweep_db(db_path: str, home: str) -> list[str]:
    """:func:`sweep` on a pool thread with the dispatching ``home`` bound: a pool thread does not inherit the
    caller's context-local home, so ``dispatcher_gh_argv`` would read the launch home's gh_command_prefix.
    Never raises: a refused bind (``ScopeSwitchRefused``) or connect error is logged here, with this sweep's
    wording, rather than as a generic pool failure in the wake-check tick. A close failure after a finished sweep
    is logged as a close failure and keeps the sweep's result."""
    from pathlib import Path

    from agent.runtime_scope import bind_profile_scope
    from hermes_cli import kanban_db_connect as kbc
    try:
        with bind_profile_scope(home, hydrate_secrets=False):
            conn = kbc.connect(Path(db_path))
            try:
                return sweep(conn)
            finally:
                try:
                    conn.close()
                except Exception:  # keep the sweep's result: only the close failed
                    logger.warning("kanban ci run progress: closing %s for home %s failed after the sweep",
                                   db_path, home, exc_info=True)
    except Exception:
        logger.warning("kanban ci run progress: sweep of %s for home %s failed", db_path, home, exc_info=True)
        return []


_CI_WAIT_SQL = (
    "SELECT 1 FROM tasks t WHERE t.status = 'scheduled' AND (SELECT CASE WHEN json_valid(e.payload) "
    "THEN json_extract(e.payload, '$.awaiting.kind') END FROM task_events e WHERE e.task_id = t.id "
    "AND e.kind IN ('scheduled', 'continued') ORDER BY e.id DESC LIMIT 1) = 'ci' LIMIT 1")


def _has_ci_wait(conn: sqlite3.Connection) -> bool:
    """Whether some scheduled card's latest park is ``kind=ci`` (a superset of :func:`_ci_waits`), in one indexed
    query. A sweep opens its own connection, which briefly takes the board's dispatch tick lock, so it starts only
    when it has a CI wait to read, not on every board with a scheduled card. [daveey]"""
    return conn.execute(_CI_WAIT_SQL).fetchone() is not None


def running_sweeps() -> int:
    """Sweeps in flight on any board: each holds a wake-check pool worker, as a probe does."""
    with _LOCK:
        return sum(1 for fut in _SWEEPS.values() if not fut.done())


def _sweep_due(db: str) -> bool:
    """No sweep of ``db`` is running and ``SWEEP_EVERY_SECONDS`` passed since its last one. Call under ``_LOCK``."""
    running = _SWEEPS.get(db)
    if running is not None and not running.done():
        return False
    last = _LAST_SWEEP.get(db)
    return last is None or time.monotonic() - last >= SWEEP_EVERY_SECONDS


def submit_sweep(conn: sqlite3.Connection, pool, *, slots_free: int = 1) -> Optional[Future]:
    """Start one :func:`sweep` of this board on ``pool`` when the pool's probe budget has ``slots_free``, no sweep
    of the board is running, ``SWEEP_EVERY_SECONDS`` passed since its last one and some scheduled card's latest park
    is ``kind=ci`` (:func:`_has_ci_wait`). Runs on the dispatcher thread, so it does only that one indexed query; the
    card scan is the sweep's. The home is resolved here, on the dispatcher thread that has the board's home bound,
    and rebound on the pool thread. Never raises."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_constants import get_hermes_home
    try:
        db = str(kbc._main_db_file(conn) or "")
        if not db or slots_free <= 0:
            return None
        home = str(get_hermes_home())
        with _LOCK:
            if not _sweep_due(db):
                return None
        if not _has_ci_wait(conn):
            return None
        with _LOCK:
            if not _sweep_due(db):  # another tick on this board started one while _LOCK was released
                return None
            _LAST_SWEEP[db] = time.monotonic()
            fut = pool.submit(_sweep_db, db, home)
            _SWEEPS[db] = fut
        return fut
    except Exception:
        logger.warning("kanban ci run progress: could not start a sweep", exc_info=True)
        return None
