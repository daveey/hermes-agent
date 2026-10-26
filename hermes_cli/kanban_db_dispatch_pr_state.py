"""PR state lookup for the respawn guard's ``active_pr`` hold. [daveey]

``active_pr`` exists to stop a second worker opening a duplicate PR. Once every PR a
card's comments name is MERGED or CLOSED there is nothing left to duplicate, so the hold
must lift (2026-10-23: t_d507533c sat held 6 h after Metta-AI/coworld-qwop#1 merged).
States and queue markers come from ``gh pr view --json state,comments``. A completed
rejection releases the worker to repair the existing PR, with a quoted card comment.
Anything unknown (gh missing, auth, network,
timeout, unparseable output) is ``None`` and the caller keeps holding (fail closed).

Results are cached per process: terminal states forever (a merged PR never reopens; a
closed one can, but a reopened PR is caught again by the next comment or after restart),
OPEN for :data:`OPEN_TTL_SECONDS`, failures for :data:`ERROR_TTL_SECONDS` so a gh outage
does not cost a subprocess per held card per tick.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"MERGED", "CLOSED"})
OPEN_TTL_SECONDS = 300
ERROR_TTL_SECONDS = 120
GH_TIMEOUT_SECONDS = 20

# url -> (state or None, monotonic expiry or None for "forever")
_cache: dict[str, tuple[Optional[str], Optional[float]]] = {}
_rejections: dict[str, str] = {}


def _gh_argv() -> Optional[list[str]]:
    """``gh`` argv for the dispatcher's PR-state read, or None when it cannot be built.

    Reuses the dispatcher home's ``kanban.pr_acceptance.gh_command_prefix`` (a credential
    broker wrapper such as ``token-broker exec --scope user/github --``) and resolves ``gh``
    from ~/bin / ~/.local/bin as the PR-acceptance gate does: a service process has a minimal
    PATH and often no ambient ``gh`` login, and without either every lookup failed closed, so
    merged PRs held their cards for the full 6 h hold cap. [daveey]"""
    try:
        from hermes_cli.kanban_pr_acceptance import _gh_command_prefix
        from hermes_platform.resolver.github import resolve_gh
        resolved = resolve_gh()
        if not resolved.found:
            return None
        return [*_gh_command_prefix(None), *resolved.command]
    except Exception:  # config/setup errors may carry credentials: log no text
        logger.debug("kanban: gh argv for PR state unavailable")
        return None


def _gh_pr_state(url: str) -> Optional[str]:
    """GitHub state, ``REJECTED`` for a completed queue rejection, or None on failure."""
    gh = _gh_argv() or ["gh"]
    try:
        proc = subprocess.run([*gh, "pr", "view", url, "--json", "state,comments"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=GH_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        logger.debug("kanban: gh pr view %s failed", url, exc_info=True)
        return None
    if proc.returncode != 0:
        logger.debug("kanban: gh pr view %s exited %s: %s", url, proc.returncode, (proc.stderr or "")[:200])
        return None
    try:
        data = json.loads(proc.stdout or "{}")
        state = data.get("state")
    except (ValueError, AttributeError):
        return None
    state = state.upper() if isinstance(state, str) and state else None
    key = url.rstrip("/").lower()
    _rejections.pop(key, None)
    if state == "OPEN":
        from hermes_cli.kanban_db_dispatch_pr_rejection import rejection_reason
        comments = data.get("comments", [])
        reason = rejection_reason(comments) if isinstance(comments, list) else None
        if reason:
            _rejections[key] = reason
            return "REJECTED"
    return state


def pr_state(url: str) -> Optional[str]:
    """Cached :func:`_gh_pr_state`."""
    key = url.rstrip("/").lower()
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and (hit[1] is None or now < hit[1]):
        return hit[0]
    state = _gh_pr_state(url)
    if state == "REJECTED":
        _cache.pop(key, None)  # requeue must invalidate rejection immediately
    elif state in TERMINAL_STATES:
        _cache[key] = (state, None)
    else:
        _cache[key] = (state, now + (OPEN_TTL_SECONDS if state else ERROR_TTL_SECONDS))
    return state


def all_prs_finished(urls: Iterable[str]) -> bool:
    """True only when ``urls`` is non-empty and every PR is MERGED or CLOSED.

    An unknown state (gh failure) counts as unfinished: duplicate-PR risk fails closed."""
    seen = {u.rstrip("/").lower(): u for u in urls}
    if not seen:
        return False
    return all(pr_state(u) in TERMINAL_STATES for u in seen.values())


def active_pr_reason(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """Respawn-guard step 4: ``"active_pr"`` when a comment in the last 24 h names a PR, else None.

    Exception: a handoff AFTER the newest PR comment (operator reassign, reviewer
    changes_requested, review reopen) names the profile that must now work on THAT PR — a
    closer or the implementer finishing it, not a duplicate implementation (#111910). So does
    an ``unblocked`` event: a worker that opened its PR and blocked itself waiting for the
    merge is resumed by that unblock to ship/verify THAT PR. So does an orderly park AFTER the
    PR comment (``continued`` checkpoint or ``scheduled`` timed wait): the owner parked knowing
    its PR, and the wake resumes it from that checkpoint, not a context-free respawn. A
    crash/reclaim is not a handoff, so the worker that opened the PR is still not re-spawned
    against it.
    Also lifted once EVERY PR named in those comments is MERGED or CLOSED (gh, cached; any
    gh failure keeps the hold), or has a completed queue rejection requiring repair. [daveey]
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    pr_times: list[int] = []
    pr_urls: list[str] = []
    for t, c in _kbd._rows_since(conn, "task_comments", task_id, now - _kbd._RESPAWN_GUARD_PR_WINDOW,
                                 cols=", body", unreadable=now):  # duplicate-PR risk: fail closed
        urls = _kbd._RESPAWN_GUARD_PR_URL_RE.findall(_kb._lossy_text(c["body"]) or "")
        if urls:
            pr_times.append(t)
            pr_urls.extend(urls)
    if not pr_times:
        return None
    # Strictly after: a same-second tie stays guarded (fail closed).
    if any(_kbd._is_handoff_event(e["kind"], e["payload"]) for _, e in _kbd._rows_since(
            conn, "task_events", task_id, max(pr_times),
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened', 'unblocked', "
            "'continued', 'scheduled')",
            cols=", kind, payload", strict=True)):
        return None
    states = {u: pr_state(u) for u in dict.fromkeys(pr_urls)}
    if all(s in TERMINAL_STATES or s == "REJECTED" for s in states.values()):
        from hermes_cli.kanban_db_dispatch_pr_rejection import comment_rejection
        for url, state in states.items():
            if state == "REJECTED":
                comment_rejection(conn, task_id, url, _rejections[url.rstrip("/").lower()])
        return None
    return "active_pr"


def clear_cache() -> None:
    _cache.clear()
    _rejections.clear()
