"""An awaited exact-head review resumes its parked owner for landing or repair. [daveey]

A PR owner that parks with ``kanban_schedule``/``kanban_continue`` waiting on a review card
(its wake-check ``hermes kanban wait-check task <id>``, or ``awaiting`` kind ``card``) is
not linked to that card as a parent, so :func:`kanban_dependency_review.resumes_review`
never sees it. When the review approved, the owner woke and the ``active_pr`` guard held it:
the PR is open only because the owner's next run is the one that merges it (2026-11-13
t_01a1d30035fa4777: review t_01a1d3093ee49e78 approved PR #1862 at its head; held 30+ min).

One continuation is permitted only when all of these hold:

* the card's :func:`kanban_pr_same_run_park.latest_parked_run` (its latest executed run,
  ended as the assignee's orderly ``scheduled``/``continued`` park) names the awaited card(s);
* each awaited card is done with a claimed completion that ended at or after the park;
* each approves or requests changes; together they cover every holding PR by name
  (``pr``/``reviewed_pr``) at a full ``reviewed_sha`` equal to that PR's current GitHub head,
  read by ``pr_state``'s own ``gh pr view`` and re-read before it releases (a cached head
  only refuses).

A change request resumes repair, not approval. Another head or PR, an unknown head
(gh failure), or a crash/reclaim after the park keeps the hold. It never authorizes a merge.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Iterable, Optional

# The whole probe must be the card-wait idiom ``kanban_awaiting.card_probe`` writes, with only
# wait-check's own flags. A wrapper (``bash -lc``, ``timeout``, a path, env) or a shell operator
# (``&& false``, ``| grep``, a second line) changes what the probe means, so it names no
# awaited card here (fail closed); the park's recorded ``awaiting`` still can. [daveey]
_WAIT_CHECK = re.compile(r"\s*hermes\s+kanban\s+wait-check\s+task\s+(t_[0-9A-Za-z_]+)"
                         r"(?:\s+--(?:gateway|board|until|from)(?:=|\s+)[A-Za-z0-9_.,:@-]+)*\s*\Z")
_TASK_ID = re.compile(r"t_[0-9A-Za-z_]+\Z")
# A head read older than this never releases a hold: re-read it (one ``gh`` call per release).
RELEASE_HEAD_MAX_AGE_SECONDS = 5


def pr_head(url: str, *, max_age: Optional[float] = None) -> Optional[str]:
    """Full head sha of an OPEN PR from :func:`pr_state`'s own read and cache entry; None when unknown."""
    from hermes_cli.kanban_db_dispatch_pr_state import pr_head as shared_pr_head

    return shared_pr_head(url, max_age=max_age)


def _at_head(url: str, sha: str) -> bool:
    """True when ``sha`` is the PR's head now. A cached head may refuse, never release: a
    force-push after the approval could still match the approved sha in a head read up to
    OPEN_TTL_SECONDS old, so a match is confirmed by a read at most seconds old."""
    return pr_head(url) == sha and pr_head(url, max_age=RELEASE_HEAD_MAX_AGE_SECONDS) == sha


def clear_cache() -> None:
    from hermes_cli.kanban_db_dispatch_pr_state import _heads

    _heads.clear()


def awaited_cards(payload: dict) -> list[str]:
    """Card ids a park waits on: ``awaiting`` kind ``card`` or a ``wait-check task`` probe."""
    found: list[str] = []
    awaiting = payload.get("awaiting")
    if isinstance(awaiting, dict) and awaiting.get("kind") == "card":
        ref = awaiting.get("ref")
        if isinstance(ref, str) and _TASK_ID.match(ref):
            found.append(ref)
    episode = payload.get("_wake_episode_v1")
    for cmd in (payload.get("wake_check"), episode.get("park_cmd") if isinstance(episode, dict) else None):
        match = _WAIT_CHECK.match(cmd) if isinstance(cmd, str) else None
        if match:
            found.append(match[1])
    return list(dict.fromkeys(found))


def _park(conn: sqlite3.Connection, task_id: str):
    """(run, park payload, parked_at) of the latest run's orderly timed park, or None."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_pr_same_run_park import latest_parked_run

    run = latest_parked_run(conn, task_id)
    if run is None:
        return None
    # Use event FKs (sync remaps them by stable run UID), never a JSON/local run id.
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'",
                    (task_id, run["id"])).fetchone() is None:
        return None
    event = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1", (task_id, run["id"], run["outcome"])).fetchone()
    parked_at = kb._to_epoch(event["created_at"]) if event is not None else None
    if parked_at is None:
        return None
    return run, kb._json_dict(event["payload"]), parked_at


def _reviewed_at_head(conn, card: str, parked_at: int, run_id: int, urls: set[str]) -> Optional[set[str]]:
    """PRs reviewed at their current head; None without a completed exact-head verdict."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_dependency_review as review

    row = review._completed_review_run(conn, card, parked_at, run_id)
    if row is None:
        return None  # an awaited card still open (or finished before the park) keeps the hold
    data = kb._json_dict(row["metadata"])
    verdict = data.get("verdict")
    sha = data.get("reviewed_sha")
    approval = isinstance(verdict, str) and verdict.strip().lower() in review._APPROVALS
    if (not (approval or review._requests_changes(row, data))
            or not isinstance(sha, str) or not review._SHA.fullmatch(sha)):
        return None  # every awaited review must supply a verdict at a full exact head
    named = review._named_prs(data, urls) or set()  # must name the PR: no checkpoint binds scope
    return {u for u in named if _at_head(u, sha.lower())}  # pr_head is a full 40-hex sha or None


def resumes_awaited_review(conn: sqlite3.Connection, task_id: str, pr_urls: Iterable[str]) -> bool:
    """One continuation of a timed park whose awaited reviews cover every holding PR at head."""
    from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr

    urls = {norm_pr(u) for u in pr_urls}
    parked = _park(conn, task_id) if urls else None
    if parked is None:
        return False
    run, payload, parked_at = parked
    covered: set[str] = set()
    for card in awaited_cards(payload):
        if card == task_id:
            continue
        reviewed = _reviewed_at_head(conn, card, parked_at, run["id"], urls)
        if not reviewed:
            return False  # unfinished, unbound or stale reviews cannot be outvoted by a sibling
        covered |= reviewed
    return urls.issubset(covered)
