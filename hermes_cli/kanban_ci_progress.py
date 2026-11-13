"""A CI wait must name a run that can start. [daveey]

On 2026-11-11 a hub card parked (``awaiting`` kind=ci) on GitHub Actions run 42993449998, whose jobs asked for
``self-hosted,ci-fast`` runners that no longer took jobs after the repo moved. ``_check_run`` only checked that the
ref looked like a run id, so the card slept until David asked why nothing was running; the run was cancelled
unstarted 16 minutes later. The shared "runners offline" blocker had been cleared from a one-time count that
morning, and nothing raised it again.

:func:`ci_run_progress` reads a GitHub Actions run's jobs and reports ``not_starting`` when one has been queued
longer than ``QUEUED_LIMIT_SECONDS`` with no runner assigned.

A job nobody picked up can also mean the runners are online but saturated. When the repo's or org's self-hosted
runner list is readable (``actions/runners``) and a runner that is ``online`` carries every label the job asks for,
the run is ``busy`` instead: a park on it is allowed and nothing is woken. When the list is unreadable (the token
lacks admin scope) the run stays ``not_starting`` and every message says only that no runner picked it up. The org
list can include runners in groups this repo may not use, so such a runner online for the labels reads as ``busy``
and nothing is woken (a false negative, never a false wake); the repo list is read first and decides alone when it
has an online match.

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
BUSY = "busy"
QUEUED_LIMIT_SECONDS = 15 * 60
RECHECK_SECONDS = 5 * 60
FETCH_TIMEOUT_SECONDS = 20
NO_CREDENTIAL_WARN_SECONDS = 60 * 60
SWEEP_EVERY_SECONDS = 60
MAX_CARDS_PER_SWEEP = 6
SWEEP_BUDGET_SECONDS = 90
BLOCKER_PREFIX = "runners-offline"
AUTHOR = "kanban-dispatcher"

_SLUG = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_RUN_PATH = re.compile(rf"(?:https?://github\.com/)?({_SLUG})/actions/runs/(\d+)")
_BARE_RUN = re.compile(r"\d{6,}")
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
    runners_readable: bool = False  # not_starting: the runner list was read and no online runner matched


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


def _fetch_runners(repo: str, labels: Optional[list[str]] = None, *,
                   deadline: Optional[float] = None) -> Optional[list]:
    """Self-hosted runners the repo can use (repo-level, then its owner's org-level). None when neither list is
    readable: listing runners needs admin scope, which a worker's token often lacks. When the repo list already has
    an online runner for ``labels`` the org list is not read. The org list may hold runners in groups this repo
    cannot use (a false ``busy``). Raises :class:`OverBudget` when ``deadline`` (monotonic) passed before a read."""
    found: Optional[list] = None
    for path in (f"repos/{repo}/actions/runners?per_page=100",
                 f"orgs/{repo.split('/', 1)[0]}/actions/runners?per_page=100"):
        if found and labels is not None and _online_runner(found, labels) is not None:
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise OverBudget(f"sweep budget spent before {path.split('?', 1)[0]} was read")
        try:
            data = _gh_api(path)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            logger.debug("ci run progress: %s unreadable: %s", path, exc)
            continue
        found = (found or []) + list((data or {}).get("runners") or [])
    return found


def _online_runner(runners: list, labels: list[str]) -> Optional[str]:
    """Name of an ``online`` runner carrying every label in ``labels`` (case-insensitive), else None."""
    want = {x.lower() for x in labels}
    for runner in runners:
        if not isinstance(runner, dict) or runner.get("status") != "online":
            continue
        have = {str(x.get("name") or "").lower() for x in runner.get("labels") or [] if isinstance(x, dict)}
        if want <= have:
            return str(runner.get("name") or "(unnamed)")
    return None


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
        name = _online_runner(runners, labels)
        if name is not None:
            logger.info("ci run %s/%s queued %ss: runner %s is online for %s, so the runners are saturated",
                        repo, run_id, age, name, ",".join(labels))
            return Progress(BUSY, repo, run_id, labels, age,
                            detail=f"runner {name} is online for those labels; the runners are saturated")
    return Progress(NOT_STARTING, repo, run_id, labels, age, runners_readable=runners is not None)


def describe(p: Progress) -> str:
    labels = ",".join(p.labels) or "(none)"
    seen = ("no online runner has them" if p.runners_readable
            else "runner list unreadable, so they may be offline or saturated")
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
    p = ci_run_progress(str(awaiting.get("ref") or ""), hint=hint, fetch=fetch, fetch_runners=fetch_runners)
    if p.state == "unknown":
        logger.warning("ci run progress for %s/%s unreadable, allowing the park: %s", p.repo, p.run_id, p.detail)
        return
    if p.state != NOT_STARTING:
        return
    from hermes_cli.kanban_awaiting import AwaitingError
    raise AwaitingError(CI_RUN_UNSTARTABLE, (
        f"{describe(p)}. A wait on it would sleep until someone notices. Get a runner with labels "
        f"{','.join(p.labels)} taking jobs, or re-run the job on runners that exist (fix the workflow's runs-on), or "
        f"park behind the shared blocker: kanban_block(kind='time', blocker_id='{blocker_id(p.labels)}', ...)."))


# --- while parked --------------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_LAST_CHECK: dict[tuple[str, str], float] = {}   # (db, task id) -> monotonic time of its last read; live waits only
_LAST_SWEEP: dict[str, float] = {}               # db -> monotonic time its last sweep started
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


def _others_behind(conn: sqlite3.Connection, task_id: str, bid: str) -> list[str]:
    """Other live cards already waiting behind the open shared blocker ``bid`` that ``shared_blocker_guard``
    counts (a provider-scoped blocker ignores cards routed to another provider)."""
    from hermes_cli import kanban_shared_blockers as ksb
    st = ksb.blocker_states(conn).get(bid)
    if st is None or not st.is_open:
        return []
    return sorted(t for t in st.behind if t != task_id and ksb._task_applies(conn, st, t))


def apply(conn: sqlite3.Connection, task_id: str, ref: str, p: Progress) -> Optional[str]:
    """Wake a card parked on a run that is not starting, comment why, and assert the shared blocker on it. The
    first card behind the blocker is released as its probe and may run at once; a later one is held with the
    others (``shared_blocker_guard``), and the comment says so."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_shared_blockers as ksb
    from hermes_cli.kanban_awaiting import _latest_park
    from hermes_cli.kanban_wake_check import _wake

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
            p = ci_run_progress(ref, hint=hint, fetch=fetch, fetch_runners=fetch_runners, deadline=deadline)
            if p.deferred:
                with _LOCK:
                    _LAST_CHECK.pop((db, task_id), None)  # least recently checked: first next sweep
                continue
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
            running = _SWEEPS.get(db)
            if running is not None and not running.done():
                return None
            last = _LAST_SWEEP.get(db)
            if last is not None and time.monotonic() - last < SWEEP_EVERY_SECONDS:
                return None
        if not _has_ci_wait(conn):
            return None
        with _LOCK:
            _LAST_SWEEP[db] = time.monotonic()
            fut = pool.submit(_sweep_db, db, home)
            _SWEEPS[db] = fut
        return fut
    except Exception:
        logger.warning("kanban ci run progress: could not start a sweep", exc_info=True)
        return None
