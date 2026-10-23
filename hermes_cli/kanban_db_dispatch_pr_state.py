"""PR state lookup for the respawn guard's ``active_pr`` hold. [daveey]

``active_pr`` exists to stop a second worker opening a duplicate PR. Once every PR a
card's comments name is MERGED or CLOSED there is nothing left to duplicate, so the hold
must lift (2026-10-23: t_d507533c sat held 6 h after Metta-AI/coworld-qwop#1 merged).
States come from ``gh pr view --json state``; anything else (gh missing, auth, network,
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


def _gh_pr_state(url: str) -> Optional[str]:
    """``MERGED`` / ``CLOSED`` / ``OPEN`` for ``url`` via gh, or ``None`` on any failure."""
    try:
        proc = subprocess.run(["gh", "pr", "view", url, "--json", "state"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=GH_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        logger.debug("kanban: gh pr view %s failed", url, exc_info=True)
        return None
    if proc.returncode != 0:
        logger.debug("kanban: gh pr view %s exited %s: %s", url, proc.returncode, (proc.stderr or "")[:200])
        return None
    try:
        state = json.loads(proc.stdout or "{}").get("state")
    except (ValueError, AttributeError):
        return None
    return state.upper() if isinstance(state, str) and state else None


def pr_state(url: str) -> Optional[str]:
    """Cached :func:`_gh_pr_state`."""
    key = url.rstrip("/").lower()
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and (hit[1] is None or now < hit[1]):
        return hit[0]
    state = _gh_pr_state(url)
    if state in TERMINAL_STATES:
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
    merge is resumed by that unblock to ship/verify THAT PR. A crash/reclaim is not a
    handoff, so the worker that opened the PR is still not re-spawned against it.
    Also lifted once EVERY PR named in those comments is MERGED or CLOSED (gh, cached; any
    gh failure keeps the hold): there is nothing left to duplicate. [daveey]
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
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened', 'unblocked')",
            cols=", kind, payload", strict=True)):
        return None
    if all_prs_finished(pr_urls):
        return None
    return "active_pr"


def clear_cache() -> None:
    _cache.clear()
