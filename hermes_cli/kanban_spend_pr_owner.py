"""Proven core publication ownership for spend-budget sparing. [daveey]

A PR mention, completion contract, review receipt or foreign comment is not publication.
Two proofs exist: ``published_pr`` run metadata (fast path), or a core PR URL in this card's
own run's comment whose PR body carries exactly one ``Hermes-Card: <this task>`` trailer
(written by canonical ship) and is OPEN on main. Unknown GitHub state, base or body, a
conflicting or duplicate trailer, and quoted lines all retain normal budget enforcement.
"""
from __future__ import annotations

import re
import sqlite3

_CORE_PR = re.compile(r"https://github\.com/softmax-universe/daveey-hermes/pull/[1-9][0-9]*")
_CORE_PR_IN_TEXT = re.compile(r"https://github\.com/softmax-universe/daveey-hermes/pull/[1-9][0-9]*(?![0-9A-Za-z_/])")
EVENT = "spend_budget_spared"
_CORE_REPO = "https://github.com/softmax-universe/daveey-hermes"
# `PR #N`, `PR N`, `PRN`, `pr#N`: discovery only, resolved to this repo and verified by the PR body trailer.
_PR_NUMBER = re.compile(r"(?<![A-Za-z0-9_])PR[ \t]*#?[ \t]*([1-9][0-9]*)(?![0-9A-Za-z_/])", re.IGNORECASE)


def published_prs(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Core PR URLs from ``published_pr`` run metadata only."""
    from hermes_cli import kanban_db as kb

    urls = set()
    for row in conn.execute("SELECT metadata FROM task_runs WHERE task_id=?", (task_id,)):
        value = kb._json_dict(row[0]).get("published_pr")
        if isinstance(value, str) and _CORE_PR.fullmatch(value):
            urls.add(value)
    return urls


def comment_candidates(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Core PR URLs on unquoted lines of this card's own run's comments (discovery only, unverified)."""
    from hermes_cli.kanban_db_dispatch_pr_state import _comment_events
    from hermes_cli.kanban_requeued_publisher import _has_run_uid, _writer_run

    events = _comment_events(conn, task_id)
    if events is None:
        return set()
    synced = _has_run_uid(conn)
    by_comment: dict[int, list[dict]] = {}
    for data, _at, cid in events:
        if cid is not None:
            by_comment.setdefault(cid, []).append(data)
    urls = set()
    for cid, body in conn.execute("SELECT id, body FROM task_comments WHERE task_id=?", (task_id,)):
        stamps = by_comment.get(cid, [])
        if len(stamps) != 1 or _writer_run(conn, task_id, stamps[0], synced) is None:
            continue
        for line in (body or "").splitlines():
            if not line.lstrip().startswith(">"):
                urls.update(_CORE_PR_IN_TEXT.findall(line))
                urls.update(f"{_CORE_REPO}/pull/{n}" for n in _PR_NUMBER.findall(line))
    return urls


def recognised_prs(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Database-only recheck set for the write transaction; the trailer is verified by :func:`open_core_pr`."""
    return published_prs(conn, task_id) | comment_candidates(conn, task_id)


def spared_publisher(conn: sqlite3.Connection, task_id: str) -> str | None:
    """The open core PR that exempts this card, or None. Read-only.

    The same check ``hold`` applies after its one capped ``open_core_pr`` read: an open PR in
    :func:`recognised_prs`. ``hold`` keeps that read outside the write lock so a second call cannot
    spend another uncached-read budget; this helper is for a diagnostic that has not read yet.
    """
    pr = open_core_pr(conn, task_id)
    if pr is not None and pr in recognised_prs(conn, task_id):
        return pr
    return None


_MAX_CANDIDATES = 20  # most recently named PRs considered per card
_MAX_READS = 8  # uncached GitHub reads per card; cached (e.g. merged) candidates are free


def _number(url: str) -> int:
    return int(url.rsplit("/", 1)[1])


def _cached(url: str) -> bool:
    import time
    from hermes_cli.kanban_db_dispatch_pr_state import OPEN_TTL_SECONDS, _details, norm_pr

    hit = _details.get(norm_pr(url))
    return hit is not None and time.monotonic() - hit[2] < OPEN_TTL_SECONDS


def open_core_pr(conn: sqlite3.Connection, task_id: str) -> str | None:
    from hermes_cli.kanban_db_dispatch_pr_state import pr_card_owners, pr_details

    for url in sorted(published_prs(conn, task_id)):
        if pr_details(url) == ("OPEN", "main"):
            return url
    reads = 0
    for url in sorted(comment_candidates(conn, task_id), key=_number, reverse=True)[:_MAX_CANDIDATES]:
        if not _cached(url):
            if reads >= _MAX_READS:
                continue  # fail closed on the rest; cached candidates are still checked
            reads += 1
        if pr_details(url) == ("OPEN", "main") and pr_card_owners(url) == (task_id,):
            return url
    return None


def record_spared(conn, task_id, pr, spent, note):
    """Caller holds the write transaction and revalidated spend, ownership and lane state."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_spend_budget import AUTHOR

    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, EVENT)):
        if kb._json_dict(row[0]).get("since") == spent.since:
            return
    kb._append_event(conn, task_id, EVENT, {"pr": pr, "runs": spent.runs, "tokens": spent.tokens,
                                          "since": spent.since, "reason": note})
    kb.add_comment(conn, task_id, AUTHOR,
                   f"{note}. Spend budget spared: this card published open core PR {pr} (base main). "
                   "Runs and tokens keep counting; normal enforcement resumes once it is merged or closed.",
                   system=True)
