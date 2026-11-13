"""A CI wait must name a run that can start. [daveey]

On 2026-11-11 a hub card parked (``awaiting`` kind=ci) on GitHub Actions run 42993449998, whose jobs asked for
``self-hosted,ci-fast`` runners that no longer took jobs after the repo moved. ``_check_run`` only checked that the
ref looked like a run id, so the card slept until David asked why nothing was running; the run was cancelled
unstarted 16 minutes later. The shared "runners offline" blocker had been cleared from a one-time count that
morning, and nothing raised it again.

:func:`ci_run_progress` reads a GitHub Actions run's jobs and reports ``not_starting`` when one has been queued
longer than ``QUEUED_LIMIT_SECONDS`` with no runner assigned.

* Park time (:func:`confirm`, called by ``kanban_awaiting.confirm_remote`` before the write txn): a ``kind=ci``
  park on such a run is refused with ``ci_run_unstartable`` naming the labels.
* While parked (:func:`sweep`, from the dispatcher's wake-check tick): each card whose latest park waits on a
  GitHub run is re-read at most every ``RECHECK_SECONDS``. On ``not_starting`` the card is woken with a comment and
  the shared blocker ``runners-offline.<labels>`` is asserted on it, so further cards that hit the same labels
  queue behind one probe instead of each sleeping.

A ref is checked when it is (or contains) ``<owner>/<repo>/actions/runs/<id>``, or is a bare run id whose wake
check names the repo (``gh run view <id> -R owner/repo``). Anything else (a PR, a release, an unknown repo) is
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
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CI_RUN_UNSTARTABLE = "ci_run_unstartable"
NOT_STARTING = "not_starting"
QUEUED_LIMIT_SECONDS = 15 * 60
RECHECK_SECONDS = 5 * 60
FETCH_TIMEOUT_SECONDS = 20
NO_CREDENTIAL_WARN_SECONDS = 60 * 60
BLOCKER_PREFIX = "runners-offline"
AUTHOR = "kanban-dispatcher"

_SLUG = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_RUN_PATH = re.compile(rf"(?:https?://github\.com/)?({_SLUG})/actions/runs/(\d+)")
_BARE_RUN = re.compile(r"\d{6,}")
_REPO_FLAG = re.compile(rf"(?:^|\s)(?:-R|--repo)(?:=|\s+)['\"]?({_SLUG})")
_GENERIC_LABELS = frozenset({"self-hosted", "linux", "macos", "windows", "x64", "arm64"})

Fetch = Callable[[str, str], dict]


class NoCredential(RuntimeError):
    """gh has no usable login here (exit 4 / HTTP 401), or the broker prefix is unusable."""


class GhMissing(RuntimeError):
    """No ``gh`` executable: the fix is installing it, not a credential."""


@dataclass
class Progress:
    state: str  # "untracked" | "unknown" | "ok" | "not_starting"
    repo: str = ""
    run_id: str = ""
    labels: list[str] = field(default_factory=list)
    queued_seconds: int = 0
    detail: str = ""
    no_credential: bool = False
    gh_missing: bool = False


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


def _fetch_jobs(repo: str, run_id: str) -> dict:
    """The run's jobs from the GitHub API through the home's broker prefix + ``gh``. Raises on any failure:
    :class:`GhMissing` with no gh; :class:`NoCredential` when gh has no usable login or the prefix is unusable
    (config text never included)."""
    from hermes_cli.kanban_gh_argv import GhArgvUnavailable, GhNotInstalled, dispatcher_gh_argv
    try:
        gh = dispatcher_gh_argv()
    except GhNotInstalled as exc:
        raise GhMissing(str(exc)) from None
    except GhArgvUnavailable as exc:
        raise NoCredential(f"gh unavailable: {exc}") from None
    p = subprocess.run([*gh, "api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"],
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


def _epoch(value) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def ci_run_progress(ref: str, *, hint: str = "", fetch: Optional[Fetch] = None,
                    now: Optional[float] = None) -> Progress:
    """Whether a CI run can still start. Never raises: an unreadable run is ``unknown``."""
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
    return Progress(NOT_STARTING, repo, run_id, labels, age)


def describe(p: Progress) -> str:
    labels = ",".join(p.labels) or "(none)"
    return (f"CI run {p.run_id} queued {p.queued_seconds // 60} min, no runner for labels {labels} ({p.repo}): no "
            "machine is picking it up, so it may never start")


def blocker_id(labels: list[str]) -> str:
    """``runners-offline.<labels>``: one shared blocker per runner pool (generic labels dropped)."""
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id
    specific = [x.lower() for x in labels if x.lower() not in _GENERIC_LABELS] or [x.lower() for x in labels]
    return normalize_blocker_id(".".join([BLOCKER_PREFIX, *specific])) or BLOCKER_PREFIX


def confirm(awaiting: Optional[dict], *, hint: str = "", fetch: Optional[Fetch] = None) -> None:
    """Refuse a ``kind=ci`` park on a run that is not starting. Network: call before the park's write txn."""
    if not awaiting or awaiting.get("kind") != "ci":
        return
    p = ci_run_progress(str(awaiting.get("ref") or ""), hint=hint, fetch=fetch)
    if p.state == "unknown":
        logger.warning("ci run progress for %s/%s unreadable, allowing the park: %s", p.repo, p.run_id, p.detail)
        return
    if p.state != NOT_STARTING:
        return
    from hermes_cli.kanban_awaiting import AwaitingError
    raise AwaitingError(CI_RUN_UNSTARTABLE, (
        f"{describe(p)}. A wait on it would sleep until someone notices. Bring a runner with labels "
        f"{','.join(p.labels)} online, or re-run the job on runners that exist (fix the workflow's runs-on), or park "
        f"behind the shared blocker: kanban_block(kind='time', blocker_id='{blocker_id(p.labels)}', ...)."))


# --- while parked --------------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_LAST_CHECK: dict[tuple[str, str], float] = {}
_SWEEPS: dict[str, Future] = {}
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
        if run_target(ref, hint):
            found.append((task_id, ref, hint))
    return found


def apply(conn: sqlite3.Connection, task_id: str, ref: str, p: Progress) -> Optional[str]:
    """Wake a card parked on a run that is not starting, comment why, and assert the shared blocker on it."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_awaiting import _latest_park
    from hermes_cli.kanban_wake_check import _wake

    note = (f"Woken: {describe(p)}. Re-parking on this run will be refused ({CI_RUN_UNSTARTABLE}). Bring a runner "
            f"for those labels online or re-run on runners that exist; other cards that hit it wait behind the "
            f"shared blocker {blocker_id(p.labels)}.")
    with kb.write_txn(conn):
        awaiting, _reason = _latest_park(conn, task_id)
        if not isinstance(awaiting, dict) or awaiting.get("kind") != "ci" or str(awaiting.get("ref") or "") != ref:
            return None  # re-parked on something else since the read
        woke = _wake(conn, task_id, CI_RUN_UNSTARTABLE, {
            "ref": ref, "repo": p.repo, "run_id": p.run_id, "labels": p.labels, "queued_seconds": p.queued_seconds,
            "note": note})
        if not woke:
            return None
        kb.add_comment(conn, task_id, AUTHOR, note)
        bid = blocker_id(p.labels)
        kb._synthesize_ended_run(conn, task_id, outcome=CI_RUN_UNSTARTABLE, summary=describe(p),
                                 metadata={"blocker_id": bid, "ci_run": ref, "labels": p.labels})
    logger.info("kanban: woke %s, %s", task_id, describe(p))
    return woke


def sweep(conn: sqlite3.Connection, *, fetch: Optional[Fetch] = None) -> list[str]:
    """Re-read every CI-waiting card's run (at most once per ``RECHECK_SECONDS`` per card) and wake the cards
    whose run is not starting. Network: run it off the dispatcher thread (``submit_sweep``). Never raises."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_constants import get_hermes_home

    woken: list[str] = []
    try:
        db = str(kbc._main_db_file(conn) or "")
        warn_key = (str(get_hermes_home()), db)
        for task_id, ref, hint in _ci_waits(conn):
            key = (db, task_id)
            with _LOCK:
                last = _LAST_CHECK.get(key)
                if last is not None and time.monotonic() - last < RECHECK_SECONDS:
                    continue
                _LAST_CHECK[key] = time.monotonic()
            p = ci_run_progress(ref, hint=hint, fetch=fetch)
            if p.no_credential or p.gh_missing:
                _warn_disabled(p, warn_key)
            elif p.state == "unknown":
                logger.warning("ci run progress for %s (%s) unreadable: %s", task_id, ref, p.detail)
            if p.state == NOT_STARTING and apply(conn, task_id, ref, p):
                woken.append(task_id)
    except Exception:
        logger.warning("kanban ci run progress sweep failed", exc_info=True)
    return woken


def _sweep_db(db_path: str, home: str) -> list[str]:
    """:func:`sweep` on a pool thread with the dispatching ``home`` bound: a pool thread does not inherit the
    caller's context-local home, so ``dispatcher_gh_argv`` would read the launch home's gh_command_prefix."""
    from pathlib import Path

    from agent.runtime_scope import bind_profile_scope
    from hermes_cli import kanban_db_connect as kbc
    with bind_profile_scope(home, hydrate_secrets=False):
        conn = kbc.connect(Path(db_path))
        try:
            return sweep(conn)
        finally:
            conn.close()


def submit_sweep(conn: sqlite3.Connection, pool) -> Optional[Future]:
    """Start one :func:`sweep` of this board on ``pool`` unless one is already running. Never raises. The home
    is resolved here, on the dispatcher thread that has the board's home bound, and rebound on the pool thread."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_constants import get_hermes_home
    try:
        db = str(kbc._main_db_file(conn) or "")
        if not db or not _ci_waits(conn):
            return None
        home = str(get_hermes_home())
        with _LOCK:
            running = _SWEEPS.get(db)
            if running is not None and not running.done():
                return None
            fut = pool.submit(_sweep_db, db, home)
            _SWEEPS[db] = fut
        return fut
    except Exception:
        logger.warning("kanban ci run progress: could not start a sweep", exc_info=True)
        return None
