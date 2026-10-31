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


def _immediate_checkpoint_after(
    conn: sqlite3.Connection, task_id: str, since: int, latest_pr_comment_id: int,
) -> bool:
    """Only the latest completed run may authorize continuing its existing PR.

    Scheduled checkpoints and crash retries retain the duplicate-work guard.
    Same-second PR reporting and checkpointing is normal within one worker run.
    """
    from hermes_cli import kanban_db as kb

    run = conn.execute(
        "SELECT id, outcome, ended_at, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if run is None or run["outcome"] != "continued":
        return False
    ended_at = kb._to_epoch(run["ended_at"])
    meta = kb._json_dict(run["metadata"])
    # A server-derived comment watermark distinguishes ordering within one second. It is a
    # row id on the host that parked, so map it to the local row first (replica cards); a
    # watermark that names no single local row falls back to strict time ordering.
    event = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'continued' ORDER BY id DESC LIMIT 1", (task_id, run["id"]),
    ).fetchone()
    watermark = kb._json_dict(event["payload"]).get("comment_id_at_checkpoint") if event else None
    if type(watermark) is not int:
        return False
    local = _local_comment_id(conn, task_id, watermark) if watermark > 0 else None
    if local is not None:
        if local < latest_pr_comment_id:
            return False
    else:
        parked_at = kb._to_epoch(event["created_at"])
        if parked_at is None or parked_at <= since:
            return False
    return bool(
        ended_at is not None and ended_at >= since
        and meta.get("checkpoint") and meta.get("resume_after_seconds") == 0
        and meta.get("resume_status") in ("ready", "review")
        and not meta.get("wake_at") and not meta.get("blocker_id")
    )


def _pr_comments_predate_first_run(conn: sqlite3.Connection, task_id: str, newest_pr_at: int) -> bool:
    """True when the card never ran a worker, or every PR comment predates its first run.

    A worker that never ran cannot have opened a duplicate PR: the PR named in a review
    card's handoff is the one it exists to review/finish.
    """
    from hermes_cli import kanban_db as _kb

    first_run = conn.execute(
        "SELECT MIN(started_at) AS t FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()["t"]
    return first_run is None or newest_pr_at < _kb._to_epoch(first_run)


def _comment_rejections(conn: sqlite3.Connection, task_id: str, urls: Iterable[str]) -> None:
    from hermes_cli.kanban_db_dispatch_pr_rejection import comment_rejection

    for url in dict.fromkeys(urls):
        if pr_state(url) == "REJECTED":
            comment_rejection(conn, task_id, url, _rejections[url.rstrip("/").lower()])


# ``add_comment`` stamps the row before taking the write lock and its ``commented`` event
# after, so the event can trail its row by lock-wait seconds (31 s seen on the hub).
COMMENT_EVENT_SKEW_SECONDS = 120


def _comment_rows(conn: sqlite3.Connection, task_id: str) -> dict[int, tuple[str, Optional[int], int]]:
    """Local ``task_comments`` rows: id -> (author, created_at epoch, len(body))."""
    from hermes_cli import kanban_db as _kb

    return {r[0]: (r[1], _kb._to_epoch(r[2]), r[3] or 0) for r in conn.execute(
        "SELECT id, author, created_at, length(body) FROM task_comments WHERE task_id = ?", (task_id,))}


def _row_matches(row: tuple[str, Optional[int], int], at: int, author: str, length: int, *, exact: bool) -> bool:
    r_author, r_at, r_len = row
    if r_at is None or r_author != author or not 0 <= at - r_at <= COMMENT_EVENT_SKEW_SECONDS:
        return False
    # The row body is stripped and the payload ``len`` is not: stripping only shortens.
    return r_len == length if exact else r_len <= length


def _event_row_id(rows: dict[int, tuple[str, Optional[int], int]], at: Optional[int],
                  data: dict) -> Optional[int]:
    """The local row a ``commented`` event describes, or None when it can't be told.

    A replica keeps the home board's events verbatim, so ``comment_id`` is the HOME row id;
    the synced local row has a different one (2026-10-30: home 6684 = hub row 9723). Match
    by (created_at, author, len). The payload id is used only when that local row has the
    same triple and no other row does, so a home board, where ids agree, maps as before.
    Two rows with the event's triple are indistinguishable: None (the caller fails closed).
    """
    author, length, cid = data.get("author"), data.get("len"), data.get("comment_id")
    if at is None or not isinstance(author, str) or type(length) is not int:
        return None
    author = author.strip()
    exact = [i for i, r in rows.items() if _row_matches(r, at, author, length, exact=True)]
    row = rows.get(cid) if type(cid) is int else None
    if row is not None and _row_matches(row, at, author, length, exact=False) \
            and all(i == cid for i in exact):
        return cid
    return exact[0] if len(exact) == 1 else None


def _comment_events(conn: sqlite3.Connection, task_id: str) -> Optional[list[tuple[dict, Optional[int], Optional[int]]]]:
    """Every ``commented`` event as (payload, event epoch, local row id or None).

    None when any event is unreadable: it cannot be associated with a comment.
    """
    from hermes_cli import kanban_db as _kb

    rows = _comment_rows(conn, task_id)
    out: list[tuple[dict, Optional[int], Optional[int]]] = []
    for payload, created_at in conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'commented'",
        (task_id,),
    ):
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or type(data.get("comment_id")) is not int \
                or not isinstance(data.get("author"), str) or type(data.get("len")) is not int:
            return None
        at = _kb._to_epoch(created_at)
        out.append((data, at, _event_row_id(rows, at, data)))
    return out


def _local_comment_id(conn: sqlite3.Connection, task_id: str, comment_id: int) -> Optional[int]:
    """Translate a payload comment id (any host's id space) to the local row it names.

    None unless every ``commented`` event carrying that id maps to one and the same row.
    """
    events = _comment_events(conn, task_id)
    if events is None:
        return None
    hits = {row for data, _at, row in events if data["comment_id"] == comment_id}
    if len(hits) != 1 or None in hits:
        return None
    return hits.pop()


def _foreign_pr_comment_ids(conn: sqlite3.Connection, task_id: str) -> set[int]:
    """Local row ids of comments from known foreign writers (FYI); absent, unreadable
    or ambiguous provenance fails closed.

    The display author is a profile shared by workers and chats, not ownership.
    Read events once per card, rather than issuing a query per comment. Events are
    matched to rows by :func:`_event_row_id`, so replica cards work too.
    """
    events = _comment_events(conn, task_id)
    if events is None:
        return set()
    foreign: set[int] = set()
    seen: set[int] = set()
    for data, _at, cid in events:
        if cid is None:
            continue  # no row is tied to it; an unmatched row stays owned (fail closed)
        if cid in seen:
            foreign.discard(cid)  # two events for one row: ambiguous provenance fails closed
            continue
        seen.add(cid)
        writer = data.get("writer_run_id")
        if "writer_run_id" not in data:
            foreign.add(cid)
        elif type(writer) is int:
            run = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (writer,)).fetchone()
            if run is not None and run[0] != task_id:
                foreign.add(cid)
    return foreign


def active_pr_reason(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """Respawn-guard step 4: ``"active_pr"`` when a comment in the last 24 h names a PR, else None.

    Exception: a handoff AFTER the newest PR comment (operator reassign, reviewer
    changes_requested, review reopen) names the profile that must now work on THAT PR — a
    closer or the implementer finishing it, not a duplicate implementation (#111910). So does
    an ``unblocked`` event: a worker that opened its PR and blocked itself waiting for the
    merge is resumed by that unblock to ship/verify THAT PR. So does an orderly park AFTER the
    PR comment (``continued`` checkpoint or ``scheduled`` timed wait): the owner parked knowing
    its PR, and the wake resumes it from that checkpoint, not a context-free respawn. So does
    a completed exact-head review of the checkpointed PRs (repair metadata, or an approval
    whose full head is ``head``/``reviewed_sha``): a later comment that repeats those PRs
    does not invalidate it. A
    crash/reclaim is not a handoff, so the worker that opened the PR is still not re-spawned
    against it. A latest immediate checkpoint also permits a same-second PR report;
    scheduled checkpoints do not get that same-second exception. Dispatcher guard
    notices and comments known to come from another card's run or a chat are
    diagnostic/FYI, not new PR publication evidence, and cannot invalidate an
    existing handoff. Missing or unreadable provenance remains fail-closed.
    Also lifted once EVERY PR named in publication comments is MERGED or CLOSED (gh, cached; any
    gh failure keeps the hold), or has a completed queue rejection requiring repair. [daveey]
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    foreign_ids = _foreign_pr_comment_ids(conn, task_id)
    pr_times: list[int] = []
    pr_urls: list[str] = []
    fyi_urls: list[str] = []
    latest_pr_comment_id = 0
    for t, c in _kbd._rows_since(conn, "task_comments", task_id, now - _kbd._RESPAWN_GUARD_PR_WINDOW,
                                 cols=", id, body, author", unreadable=now):  # duplicate-PR risk: fail closed
        body = _kb._lossy_text(c["body"]) or ""
        if c["author"] == "kanban-dispatcher" and body.startswith("[respawn-guard]"):
            continue  # a diagnostic must not rearm its own handoff barrier
        urls = _kbd._RESPAWN_GUARD_PR_URL_RE.findall(body)
        if c["id"] in foreign_ids:
            fyi_urls.extend(urls)
            continue
        if urls:
            latest_pr_comment_id = max(latest_pr_comment_id, c["id"])
            pr_times.append(t)
            pr_urls.extend(urls)
    if not pr_times:
        # Foreign handoffs may still carry queue-rejection repair instructions,
        # but cannot create a duplicate-publication hold.
        _comment_rejections(conn, task_id, fyi_urls)
        return None
    if _pr_comments_predate_first_run(conn, task_id, max(pr_times)):
        # Not held, but a queue-rejected PR in the handoff still gets its repair note,
        # exactly as when the guard releases a worker that opened it.
        from hermes_cli.kanban_db_dispatch_pr_rejection import comment_rejection
        for url in dict.fromkeys(pr_urls):
            if pr_state(url) == "REJECTED":
                comment_rejection(conn, task_id, url, _rejections[url.rstrip("/").lower()])
        return None
    if _immediate_checkpoint_after(conn, task_id, max(pr_times), latest_pr_comment_id):
        return None
    # Strictly after: a same-second handoff tie stays guarded (fail closed).
    if any(_kbd._is_handoff_event(e["kind"], e["payload"]) for _, e in _kbd._rows_since(
            conn, "task_events", task_id, max(pr_times),
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened', 'unblocked', "
            "'continued', 'scheduled')",
            cols=", kind, payload", strict=True)):
        return None
    from hermes_cli.kanban_dependency_review import resumes_review
    if resumes_review(conn, task_id, pr_urls):
        return None
    states = {u: pr_state(u) for u in dict.fromkeys(pr_urls)}
    discharged = {u for u, s in states.items() if s in TERMINAL_STATES or s == "REJECTED"}
    if len(discharged) == len(states) or resumes_review(conn, task_id, pr_urls, discharged=discharged):
        from hermes_cli.kanban_db_dispatch_pr_rejection import comment_rejection
        for url, state in states.items():
            if state == "REJECTED":
                comment_rejection(conn, task_id, url, _rejections[url.rstrip("/").lower()])
        return None
    return "active_pr"


def clear_cache() -> None:
    _cache.clear()
    _rejections.clear()
