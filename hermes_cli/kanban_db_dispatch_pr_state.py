"""PR state lookup for the respawn guard's ``active_pr`` hold. [daveey]

``active_pr`` exists to stop a second worker opening a duplicate PR. Once every PR a
card's comments name is MERGED or CLOSED there is nothing left to duplicate, so the hold
must lift (2026-10-23: t_d507533c sat held 6 h after Metta-AI/coworld-qwop#1 merged).
States and queue markers come from ``gh pr view --json state,comments,headRefOid`` (the head
serves :func:`pr_head` from the same read). A completed
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
import re
import sqlite3
import subprocess
import time
from typing import Iterable, Optional

from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"MERGED", "CLOSED"})
OPEN_TTL_SECONDS = 300
ERROR_TTL_SECONDS = 120
GH_TIMEOUT_SECONDS = 20

# url -> (state or None, monotonic expiry or None for "forever")
_cache: dict[str, tuple[Optional[str], Optional[float]]] = {}
_rejections: dict[str, str] = {}
# Raw GitHub state/base, sharing the state lookup and its TTL (REJECTED is still OPEN on GitHub).
_details: dict[str, tuple[Optional[str], Optional[str], float]] = {}
# url -> (OPEN head sha or None, monotonic time of the gh read that saw it)
_heads: dict[str, tuple[Optional[str], float]] = {}
_SHA = re.compile(r"[0-9a-fA-F]{40}")
# `Hermes-Card:` ownership trailers in the PR body (same grammar as canonical ship's writer; an empty value is kept).
_CARD_TRAILER = re.compile(r"^Hermes-Card:[ \t]*(\S*)[ \t]*\r?$", re.MULTILINE)
# url -> (trailer values in body order or None when the body was unreadable, monotonic time of the read)
_owners: dict[str, tuple[Optional[tuple[str, ...]], float]] = {}


def _gh_argv() -> Optional[list[str]]:
    """``gh`` argv for the dispatcher's PR-state read, or None when it cannot be built.

    Reuses the dispatcher home's ``kanban.pr_acceptance.gh_command_prefix`` (a credential
    broker wrapper such as ``token-broker exec --scope user/github --``) and resolves ``gh``
    from ~/bin / ~/.local/bin as the PR-acceptance gate does: a service process has a minimal
    PATH and often no ambient ``gh`` login, and without either every lookup failed closed, so
    merged PRs held their cards for the full 6 h hold cap. Built by the shared
    :func:`hermes_cli.kanban_gh_argv.dispatcher_gh_argv`. [daveey]"""
    try:
        from hermes_cli.kanban_gh_argv import dispatcher_gh_argv
        return dispatcher_gh_argv()
    except Exception:  # config/setup errors may carry credentials: log no text
        logger.debug("kanban: gh argv for PR state unavailable")
        return None


def _gh_pr_state(url: str) -> Optional[str]:
    """GitHub state, ``REJECTED`` for a completed queue rejection, or None on failure.

    The same read records the OPEN head for :func:`pr_head` (``_heads``), so the awaited-review
    release costs no second ``gh pr view``. [daveey]"""
    _details.pop(norm_pr(url), None)  # a failed refresh must not retain an old OPEN/base
    _owners.pop(norm_pr(url), None)
    gh = _gh_argv() or ["gh"]
    try:
        proc = subprocess.run([*gh, "pr", "view", url, "--json", "state,comments,headRefOid,baseRefName,body"],
                              stdin=subprocess.DEVNULL,
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
    key = norm_pr(url)
    oid = data.get("headRefOid")
    base = data.get("baseRefName")
    _details[key] = (state, base if isinstance(base, str) else None, time.monotonic())
    body = data.get("body")
    _owners[key] = (tuple(_CARD_TRAILER.findall(body)) if isinstance(body, str) else None, time.monotonic())
    _heads[key] = (oid.lower() if state == "OPEN" and isinstance(oid, str) and _SHA.fullmatch(oid) else None,
                   time.monotonic())
    _rejections.pop(key, None)
    if state == "OPEN":
        from hermes_cli.kanban_db_dispatch_pr_rejection import rejection_reason
        comments = data.get("comments", [])
        reason = rejection_reason(comments) if isinstance(comments, list) else None
        if reason:
            _rejections[key] = reason
            return "REJECTED"
    return state


def _store(key: str, state: Optional[str], now: float) -> None:
    if state == "REJECTED":
        _cache.pop(key, None)  # requeue must invalidate rejection immediately
    elif state in TERMINAL_STATES:
        _cache[key] = (state, None)
    else:
        _cache[key] = (state, now + (OPEN_TTL_SECONDS if state else ERROR_TTL_SECONDS))


def pr_state(url: str) -> Optional[str]:
    """Cached :func:`_gh_pr_state`."""
    key = norm_pr(url)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and (hit[1] is None or now < hit[1]):
        return hit[0]
    state = _gh_pr_state(url)
    _store(key, state, now)
    return state


def pr_details(url: str) -> tuple[Optional[str], Optional[str]]:
    """Raw state and base from the existing cached read; unknown never proves an exemption."""
    hit = _details.get(norm_pr(url))
    if hit is not None and time.monotonic() - hit[2] < OPEN_TTL_SECONDS:
        return hit[0], hit[1]
    if hit is not None and hit[0] == "CLOSED":
        # CLOSED can reopen: spend decisions need a bounded observation even though
        # the historical duplicate-PR guard retains terminal results until restart.
        _store(norm_pr(url), _gh_pr_state(url), time.monotonic())
    else:
        pr_state(url)
    hit = _details.get(norm_pr(url))
    return (hit[0], hit[1]) if hit is not None else (None, None)


def pr_card_owners(url: str) -> Optional[tuple[str, ...]]:
    """`Hermes-Card:` trailer values of the PR body from the cached read; None when unknown (fail closed).

    Call after :func:`pr_details`, which performs or reuses the one ``gh pr view``. [daveey]"""
    hit = _owners.get(norm_pr(url))
    if hit is None or time.monotonic() - hit[1] >= OPEN_TTL_SECONDS:
        return None
    return hit[0]


def pr_head(url: str, *, max_age: Optional[float] = None) -> Optional[str]:
    """Full lowercase head sha of an OPEN PR, from :func:`pr_state`'s read; None when unknown.

    Serves the head recorded by the last ``gh pr view`` of this PR while it is younger than
    ``max_age`` (default: :data:`OPEN_TTL_SECONDS` for a head, :data:`ERROR_TTL_SECONDS` for
    unknown), else re-reads, refreshing ``pr_state``'s cache entry too. [daveey]"""
    key = norm_pr(url)
    now = time.monotonic()
    hit = _heads.get(key)
    if hit is not None:
        limit = max_age if max_age is not None else (OPEN_TTL_SECONDS if hit[0] else ERROR_TTL_SECONDS)
        if now - hit[1] < limit:
            return hit[0]
    _heads.pop(key, None)
    _store(key, _gh_pr_state(url), now)
    _heads.setdefault(key, (None, now))  # a failed read is unknown: cached like an error state
    return _heads[key][0]


def all_prs_finished(urls: Iterable[str]) -> bool:
    """True only when ``urls`` is non-empty and every PR is MERGED or CLOSED.

    An unknown state (gh failure) counts as unfinished: duplicate-PR risk fails closed."""
    seen = {norm_pr(u): u for u in urls}
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
    from hermes_cli.kanban_core_update_requeue import latest_ended_run_skipping_requeues

    # A core-update requeue or an unused Board grant after the checkpoint is not a newer run. [daveey]
    run = latest_ended_run_skipping_requeues(conn, task_id)
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
            comment_rejection(conn, task_id, url, _rejections[norm_pr(url)])


# ``add_comment`` stamps the row before taking the write lock and its ``commented`` event
# after, so the event can trail its row by lock-wait seconds (31 s seen on the hub).
COMMENT_EVENT_SKEW_SECONDS = 120


class _Rows(dict):
    """id -> (author, created_at epoch, len(body)), plus ``uids``: portable uid -> local row ids."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.uids: dict[str, list[int]] = {}


def _comment_rows(conn: sqlite3.Connection, task_id: str) -> _Rows:
    """Local ``task_comments`` rows: id -> (author, created_at epoch, len(body))."""
    from hermes_cli import kanban_db as _kb

    has_uid = any(c[1] == "uid" for c in conn.execute("PRAGMA table_info(task_comments)"))
    out = _Rows()
    for r in conn.execute(
            f"SELECT id, author, created_at, length(body){', uid' if has_uid else ''} "
            "FROM task_comments WHERE task_id = ?", (task_id,)):
        out[r[0]] = (r[1], _kb._to_epoch(r[2]), r[3] or 0)
        if has_uid and isinstance(r[4], str) and r[4]:
            out.uids.setdefault(r[4], []).append(r[0])
    return out


def _row_matches(row: tuple[str, Optional[int], int], at: int, author: str, length: int, *, exact: bool) -> bool:
    r_author, r_at, r_len = row
    if r_at is None or r_author != author or not 0 <= at - r_at <= COMMENT_EVENT_SKEW_SECONDS:
        return False
    # Legacy payloads counted unstripped bodies; new payloads match the stored stripped length.
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
    # The portable uid names the row on every host; only a unique local match counts, else the
    # triple below decides as before (never fail open).
    uid = data.get("comment_uid")
    if isinstance(uid, str) and uid:
        hits = getattr(rows, "uids", {}).get(uid, [])
        if len(hits) == 1:
            return hits[0]
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
        if "writer_run_uid" in data:
            # Portable identity decides; never fall back to the database-local integer,
            # which on a replica can name another card's run. Unusable/unknown: owned.
            uid = data["writer_run_uid"]
            try:
                run = conn.execute("SELECT task_id FROM task_runs WHERE uid = ?", (uid,)).fetchone() \
                    if isinstance(uid, str) and uid else None
            except sqlite3.OperationalError:
                run = None  # no uid column here: provenance unreadable, stays owned
            if run is not None and run[0] != task_id:
                foreign.add(cid)
        elif "writer_run_id" not in data:
            foreign.add(cid)
        elif type(writer) is int:
            run = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (writer,)).fetchone()
            if run is not None and run[0] != task_id:
                foreign.add(cid)
    return foreign


def _park_watermark_covers(conn: sqlite3.Connection, task_id: str, evidence: object,
                           latest_pr_comment_id: int) -> bool:
    """True when the park's authenticated comment watermark includes the newest PR comment.

    The watermark is a row id on the host that parked; map it to the local row first
    (replica cards). A watermark naming no single local row proves nothing.
    """
    watermark = evidence.get("comment_id_at_checkpoint") if isinstance(evidence, dict) else None
    if type(watermark) is not int or watermark <= 0 or latest_pr_comment_id <= 0:
        return False
    local = _local_comment_id(conn, task_id, watermark)
    return local is not None and local >= latest_pr_comment_id


def _dependency_handoff_after(conn: sqlite3.Connection, task_id: str, since: int,
                              latest_pr_comment_id: int = 0) -> bool:
    """A claimed owner's orderly dependency park permits exactly one continuation.

    Operator links/synthetic runs are not owner intent. Compare occurrence times,
    not insertion order: synced history can append older events after promotion.
    A park in the same second as the newest PR comment counts only when the park's
    server-stamped comment watermark covers that comment (the owner reported, then
    parked); without it the tie stays guarded. Only awaited parents' results can
    refuse it: a review parent that finished before the park, unchanged since, was
    already in the owner's context.
    """
    from hermes_cli import kanban_db as kb

    from hermes_cli.kanban_core_update_requeue import latest_run_skipping_requeues, requeued_run_ids

    # A core-update requeue is a pause, not the consuming execution (kanban_core_update_requeue).
    run = latest_run_skipping_requeues(conn, task_id)
    task = kb.get_task(conn, task_id)
    if run is not None and (task is None or run["profile"] != task.assignee):
        return False
    if run is None or run["outcome"] != "blocked" or run["ended_at"] is None:
        return False
    ended = kb._to_epoch(run["ended_at"])
    events = list(conn.execute(
        "SELECT id, kind, payload, run_id, created_at FROM task_events WHERE task_id = ? "
        "AND kind IN ('dependency_wait', 'promoted', 'claimed', 'crashed', 'reclaimed', 'timed_out')",
        (task_id,),
    ))
    timed: list[tuple[int, sqlite3.Row]] = []
    for event in events:
        at = kb._to_epoch(event["created_at"])
        if at is None:
            return False
        timed.append((at, event))
    parks = [(at, e) for at, e in timed
             if e["kind"] == "dependency_wait" and e["run_id"] == run["id"]]
    if not parks:
        return False
    parked_at, park = max(parks, key=lambda pair: (pair[0], pair[1]["id"]))
    payload = kb._json_dict(park["payload"])
    evidence = payload.get("review_checkpoint")
    # The event's run_id identifies the run closed, not the writer: an operator
    # can close that same run. Only the authenticated expected_run_id path stamps
    # this checkpoint (for ordering dependencies as well as reviews).
    # Match by the run's stable UID (strict when present; legacy integer only when no
    # UID): sync remaps integer run ids but leaves this JSON payload unchanged.
    from hermes_cli.kanban_dependency_review import _checkpoint_matches_run
    if not _checkpoint_matches_run(evidence, run):
        return False
    if (parked_at < since or ended is None or ended < since
            or payload.get("kind") != "dependency" or payload.get("source_status") not in {"ready", "review"}):
        return False
    if parked_at == since and not _park_watermark_covers(conn, task_id, evidence, latest_pr_comment_id):
        return False  # same-second tie with no ordering proof stays guarded (fail closed)
    if not any(e["kind"] == "claimed" and e["run_id"] == run["id"] and at <= parked_at
               for at, e in timed):
        return False
    requeued = requeued_run_ids(conn, task_id)
    if any(e["kind"] in {"crashed", "reclaimed", "timed_out"} and at >= parked_at
           and e["run_id"] not in requeued
           for at, e in timed):
        return False
    if not any(e["kind"] == "promoted" and at >= ended
               and (at, e["id"]) > (parked_at, park["id"]) for at, e in timed):
        return False
    # A promotion alone is not proof: the dependency edges must still exist and
    # be satisfied, rather than having been removed by an operator. A gate an
    # operator ADDED after the park only delays the continuation (it must be done
    # too, and its own review verdict still refuses below); it is not a bypass.
    parents = kb.parent_ids(conn, task_id)
    checkpointed = evidence.get("parents")
    if (not parents or not isinstance(checkpointed, list) or not checkpointed
            or not all(isinstance(p, str) for p in checkpointed)
            or not set(checkpointed).issubset(parents)
            or kb.unsatisfied_parents(conn, task_id)):
        return False
    from hermes_cli.kanban_dependency_review import _awaited_parents, is_review_result
    awaited_now = _awaited_parents(conn, parents, evidence, parked_at)
    if not awaited_now:
        return False
    awaited = evidence.get("awaited")
    # Results is_review_result calls reviews (incomplete or invalid verdicts included)
    # belong to resumes_review: this handoff must not bypass its exact-head checks. A
    # parent proven finished and unchanged before the park is not awaited: its verdict
    # predates the park, so it neither authorizes nor refuses (2026-11-13
    # t_01a1d09d3547337b: an approval of PR #1760 that preceded the owner's rollout park
    # held the owner 30 min after the rollout finished). [daveey]
    for parent in awaited_now:
        result = conn.execute(
            "SELECT metadata, summary FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (parent,),
        ).fetchone()
        if result is None:
            from hermes_cli.kanban_move_parent_evidence import result as moved_parent_result
            result = moved_parent_result(conn, task_id, parent)
        if result is None:
            # A synced parent another gateway owns has no local run to read. When the
            # owner's checkpoint names it as already done at park time (in parents,
            # not awaited), its result predates the park and cannot authorize or
            # refuse this continuation. Anything else stays fail-closed. [daveey]
            if (isinstance(awaited, list) and parent in checkpointed and parent not in awaited
                    and kb.get_task(conn, parent) is None):
                continue
            return False
        if is_review_result(kb._json_dict(result["metadata"]), result["summary"], evidence.get("prs")):
            return False
    return True


def _handed_off_after(conn: sqlite3.Connection, task_id: str, since: int, latest_pr_comment_id: int,
                      pr_urls: list[str], tie_comment_ids: tuple[int, ...] = ()) -> bool:
    """Any supported handoff strictly after the newest PR comment (see :func:`active_pr_reason`).

    ``tie_comment_ids`` are the PR comments at ``since``: the run that wrote them and then
    parked in that same second handed them off (:mod:`kanban_pr_same_run_park`)."""
    from hermes_cli import kanban_db_dispatch as _kbd
    from hermes_cli.kanban_db_dispatch_rerun import is_handoff_event, reopened_after
    from hermes_cli.kanban_hold_release import released_after
    from hermes_cli.kanban_operator_resume import resumed_after
    from hermes_cli.kanban_pr_same_run_park import reported_then_parked

    if released_after(conn, task_id, since, pr_urls):
        return True
    if resumed_after(conn, task_id, since):
        return True  # operator `kanban resume`: one continuation of the latest run
    if _immediate_checkpoint_after(conn, task_id, since, latest_pr_comment_id):
        return True
    # Strictly after: a same-second handoff tie stays guarded (fail closed), except the
    # publishing run's own orderly park ordered after its comments.
    if any(is_handoff_event(e["kind"], e["payload"]) for _, e in _kbd._rows_since(
            conn, "task_events", task_id, since,
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened', 'unblocked', "
            "'continued', 'scheduled')",
            cols=", kind, payload", strict=True)):
        return True
    if reported_then_parked(conn, task_id, since, tie_comment_ids):
        return True
    if reopened_after(conn, task_id, since):
        return True  # operator reopen of the PR owner: it resumes THAT PR (its comments are in context)
    return _dependency_handoff_after(conn, task_id, since, latest_pr_comment_id)


def active_pr_reason(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """Respawn-guard step 4: ``"active_pr"`` when a comment in the last 24 h names a PR, else None.

    Exception: a handoff AFTER the newest PR comment (operator reassign, reviewer
    changes_requested, review reopen) names the profile that must now work on THAT PR — a
    closer or the implementer finishing it, not a duplicate implementation (#111910). So does
    an ``unblocked`` event: a worker that opened its PR and blocked itself waiting for the
    merge is resumed by that unblock to ship/verify THAT PR. So does an orderly park AFTER the
    PR comment (``continued`` checkpoint or ``scheduled`` timed wait): the owner parked knowing
    its PR, and the wake resumes it from that checkpoint, not a context-free respawn.
    The parking run's own note written just after that park, naming only PRs this card
    already named or the park itself names (its awaiting ref, reason or ship CI probe),
    belongs to the park and does not post-date it
    (:mod:`hermes_cli.kanban_pr_park_note`); any other run's note, a new PR or unreadable
    provenance keeps the hold.
    So does a supported operator reopen of the done card (``reopened``, strictly after): the
    rerun resumes THAT PR; its ``REOPENED:`` note carries chat provenance, so it is FYI here.
    So does a core-update requeue of the very run that wrote every holding PR comment since its
    claim (:mod:`hermes_cli.kanban_requeued_publisher`): one continuation, consumed by the next
    real claim.
    An ordering ``dependency_wait`` strictly after the newest PR comment also permits
    one continuation after parent completion/promotion: it must belong to the latest
    claimed run of this card and assignee, ended orderly as blocked, with no subsequent
    crash/reclaim/timeout. Operator-only links or synthetic unclaimed runs do not count;
    the next execution claim consumes the handoff (a proven unused grant or a hermes-sync
    core-update restart-safe requeue does not: :mod:`kanban_core_update_requeue`).
    A same-second dependency park counts only when
    its server-stamped comment watermark covers the newest PR comment; otherwise it stays guarded.
    This does not change the separate review-checkpoint exception below. So does
    a completed exact-head review of the checkpointed PRs (repair metadata, including
    absent/unrecognised verdicts, or an approval
    whose full head is ``head``/``reviewed_sha``): a later comment that repeats those PRs
    does not invalidate it. A
    crash/reclaim is not a handoff, so the worker that opened the PR is still not re-spawned
    against it. A latest immediate checkpoint also permits a same-second PR report;
    scheduled checkpoints do not get that same-second exception. Dispatcher guard
    notices and comments known to come from another card's run or a chat are
    diagnostic/FYI, not new PR publication evidence, and cannot invalidate an
    existing handoff. Missing or unreadable provenance remains fail-closed.
    Among the remaining candidates, another live card's completion_contract pull and a
    pull named only as not blocking this card do not hold; this card's own
    completion_contract always does (:mod:`kanban_db_dispatch_pr_policy`).
    Also lifted once EVERY PR named in publication comments is MERGED or CLOSED (gh, cached; any
    gh failure keeps the hold), or has a completed queue rejection requiring repair. [daveey]
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    from hermes_cli.kanban_db_dispatch_pr_policy import holding_urls
    from hermes_cli.kanban_dependency_review import resumes_review

    foreign_ids = _foreign_pr_comment_ids(conn, task_id)
    fyi_urls: list[str] = []
    candidates: list[tuple[int, int, str, list[str]]] = []
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
            candidates.append((t, c["id"], body, urls))
    # Own/unknown candidates only: drop another live card's contract pull and pulls named
    # only as not blocking this card; only comments that keep a pull order the watermark.
    holding = holding_urls(conn, task_id, [(u, body) for _, _, body, urls in candidates for u in urls])
    kept_by_comment = [(t, comment_id, [u for u in urls if norm_pr(u) in holding])
                       for t, comment_id, _body, urls in candidates]
    kept_by_comment = [c for c in kept_by_comment if c[2]]
    # The parked run's own post-park note restating known PRs is part of its park. [daveey]
    from hermes_cli.kanban_pr_park_note import restated_after_park
    restated, parked_at = restated_after_park(conn, task_id, kept_by_comment)
    pr_times: list[int] = []
    pr_urls: list[str] = []
    published: list[tuple[int, int]] = []
    latest_pr_comment_id = 0
    for t, comment_id, kept in kept_by_comment:
        pr_urls.extend(kept)
        if comment_id in restated:
            pr_times.append(parked_at - 1)  # ordered before the park it belongs to
            continue
        latest_pr_comment_id = max(latest_pr_comment_id, comment_id)
        pr_times.append(t)
        published.append((t, comment_id))
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
                comment_rejection(conn, task_id, url, _rejections[norm_pr(url)])
        return None
    since = max(pr_times)
    ties = tuple(cid for t, cid in published if t == since)
    if _handed_off_after(conn, task_id, since, latest_pr_comment_id, pr_urls, ties):
        return None
    from hermes_cli import kanban_requeued_publisher
    if kanban_requeued_publisher.continues(conn, task_id, published, latest_pr_comment_id):
        return None  # its own publisher was paused by a core-update requeue: one continuation
    from hermes_cli.kanban_review_release import released
    if released(conn, task_id, pr_urls) or resumes_review(conn, task_id, pr_urls):
        return None
    from hermes_cli.kanban_awaited_review import resumes_awaited_review
    if resumes_awaited_review(conn, task_id, pr_urls):
        return None
    states = {u: pr_state(u) for u in dict.fromkeys(pr_urls)}
    discharged = {u for u, s in states.items() if s in TERMINAL_STATES or s == "REJECTED"}
    if len(discharged) == len(states) or resumes_review(conn, task_id, pr_urls, discharged=discharged):
        from hermes_cli.kanban_db_dispatch_pr_rejection import comment_rejection
        for url, state in states.items():
            if state == "REJECTED":
                comment_rejection(conn, task_id, url, _rejections[norm_pr(url)])
        return None
    from hermes_cli.kanban_pr_idle_green import releases
    if releases(conn, task_id, states):
        return None  # finish this idle owner's green bound PR; landing gates remain intact
    return "active_pr"


def clear_cache() -> None:
    from hermes_cli import kanban_pr_idle_green
    kanban_pr_idle_green.clear_cache()
    _cache.clear()
    _rejections.clear()
    _heads.clear()
    _details.clear()
    _owners.clear()
