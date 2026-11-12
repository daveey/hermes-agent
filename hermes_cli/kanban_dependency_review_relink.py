"""Re-reviews an orchestrator linked after the owner's review checkpoint. [daveey]

An owner's ``dependency_wait`` checkpoint names the parents it awaited. When that review
requests changes and someone else (an orchestrator, a chat) links a fresh review card
with ``kanban_link``, no owner park ever names the new parent, so the parent set no
longer equals the checkpoint's and the owner stayed ``active_pr``-held after the
re-review approved (2026-11-12: t_01a1cde626be2ada, PR #1223 held although re-review
t_01a1ce6ea64f8fce approved head 98a1cff1).

A parent missing from the checkpoint counts as awaited only when all of these hold:

* every ``linked``/``unlinked`` event for it on the owner's card is strictly after the
  park (a same-second link, or one before the park, stays held);
* it completed (claimed run, after the park) with verdict approve or a change request;
* its metadata names, via ``pr``/``reviewed_pr``, a PR the checkpoint captured;
* its ``reviewed_sha`` equals that PR's current GitHub head.

``resumes_review`` still applies every other check, and the next claim consumes the
continuation. Anything unreadable (event payloads, gh, metadata) keeps the hold.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
HEAD_TTL_SECONDS = 60
GH_TIMEOUT_SECONDS = 20
_heads: dict[str, tuple[Optional[str], float]] = {}


def _gh_pr_head(url: str) -> Optional[str]:
    """The PR's current ``headRefOid`` from gh, or None on any failure."""
    from hermes_cli.kanban_db_dispatch_pr_state import _gh_argv

    gh = _gh_argv() or ["gh"]
    try:
        proc = subprocess.run([*gh, "pr", "view", url, "--json", "headRefOid"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=GH_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        logger.debug("kanban: gh pr view %s headRefOid failed", url, exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    try:
        head = json.loads(proc.stdout or "{}").get("headRefOid")
    except (ValueError, AttributeError):
        return None
    return head.lower() if isinstance(head, str) and _SHA.fullmatch(head) else None


def pr_head(url: str) -> Optional[str]:
    """Cached :func:`_gh_pr_head` (successes and failures alike, briefly)."""
    from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr

    key, now = norm_pr(url), time.monotonic()
    hit = _heads.get(key)
    if hit is not None and now < hit[1]:
        return hit[0]
    head = _gh_pr_head(url)
    _heads[key] = (head, now + HEAD_TTL_SECONDS)
    return head


def clear_cache() -> None:
    _heads.clear()


def _linked_after(conn, task_id: str, parent_id: str, parked_at: int) -> bool:
    from hermes_cli import kanban_db as kb

    times = []
    for event in conn.execute("SELECT created_at, payload FROM task_events WHERE task_id = ? "
                              "AND kind IN ('linked', 'unlinked')", (task_id,)):
        payload = event["payload"]
        try:
            data = json.loads(payload) if payload else None
        except (TypeError, ValueError):
            return False  # unreadable provenance: fail closed
        if not isinstance(data, dict) or not isinstance(data.get("parent"), str):
            return False
        if data["parent"] == parent_id:
            times.append(kb._to_epoch(event["created_at"]))
    return bool(times) and all(t is not None and t > parked_at for t in times)


def _current_head_review(conn, parent_id, parked_at, owner_run_id, captured: set[str]) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_dependency_review as review

    row = review._completed_review_run(conn, parent_id, parked_at, owner_run_id)
    if row is None:
        return False
    data = kb._json_dict(row["metadata"])
    verdict = data.get("verdict")
    approve = isinstance(verdict, str) and verdict.strip().lower() in review._APPROVALS
    if not (approve or review._requests_changes(row, data)):
        return False
    sha = data.get("reviewed_sha")
    named = review._named_prs(data, captured)
    if not isinstance(sha, str) or not named:
        return False  # a late parent has no checkpoint binding: it must name the PR itself
    # pr_head only returns a 40-hex head, so equality also proves sha is a full head.
    return all(pr_head(url) == sha.lower() for url in named)


def late_review_parents(conn, task_id: str, late: set[str], parked_at: int, owner_run_id: int,
                        captured: set[str]) -> bool:
    """True when every parent in ``late`` (non-empty, absent from the checkpoint) is an
    exact-head re-review of a checkpointed PR linked after the park."""
    return all(
        _linked_after(conn, task_id, p, parked_at)
        and _current_head_review(conn, p, parked_at, owner_run_id, captured)
        for p in sorted(late)
    )
