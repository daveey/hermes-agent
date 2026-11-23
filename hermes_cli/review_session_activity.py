"""Which sessions are waiting on a code review.

A session that opened a PR and is waiting for someone to approve it finished
its turn (green "done" stripe) or keeps a watcher running (no stripe), so the
sidebar never said a human was needed. Session-list endpoints now stamp each
row whose latest assistant message links an open GitHub PR still needing a
review (``reviewDecision == REVIEW_REQUIRED``) with ``review_wait``:

* ``"stuck"`` (red): the review is on the viewer — they are a requested
  reviewer, nobody is requested at all, or the other reviewers were asked
  more than 24 hours ago;
* ``"parked"`` (grey, like ⌘B): only other people are requested and the
  latest request is under 24 hours old.

Only PRs the viewer authored or was asked to review count, so a message that
merely cites someone else's PR changes nothing. [daveey]

The list call never blocks on GitHub: PR state is fetched with ``gh`` on a
background thread and cached for a few minutes; a row gets its stamp on the
next poll once the lookup lands. Message reads are cached per
(row, last_active, message_count). Never raises.
"""

from __future__ import annotations
from hermes_cli.kanban_annotation_facts import consumer as _fact_consumer

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

_log = logging.getLogger(__name__)

PARK_WINDOW_S = 24 * 3600
_PR_TTL_S = 300.0
_PR_ERROR_TTL_S = 120.0
_MAX_PRS_PER_MESSAGE = 5

_PR_URL = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.IGNORECASE)

_QUERY = """
query($o:String!,$r:String!,$n:Int!){
  viewer{login}
  repository(owner:$o,name:$r){pullRequest(number:$n){
    state isDraft reviewDecision createdAt author{login}
    reviewRequests(first:20){nodes{requestedReviewer{__typename ... on User{login} ... on Team{slug} ... on Bot{login}}}}
    timelineItems(itemTypes:[REVIEW_REQUESTED_EVENT],last:30){nodes{... on ReviewRequestedEvent{
      createdAt requestedReviewer{__typename ... on User{login} ... on Team{slug} ... on Bot{login}}}}}
  }}
}
"""

_lock = threading.Lock()
# (owner, repo, number) -> (expires_monotonic, info | None)
_pr_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any] | None]] = {}
_inflight: set[tuple[str, str, int]] = set()
_refresh_listeners: set[Callable] = set()


def subscribe_refresh(callback):
    """Native capture wake only; callbacks must not perform reads or projection."""
    with _lock:
        _refresh_listeners.add(callback)
    def close():
        with _lock:
            _refresh_listeners.discard(callback)
    return close
# row id -> ((last_active, message_count), [pr keys])
_msg_cache: dict[str, tuple[tuple[Any, Any], list[tuple[str, str, int]]]] = {}

# Test seams: replace the GitHub fetch and run lookups synchronously.
_fetch_override: Callable[[tuple[str, str, int]], dict[str, Any] | None] | None = None
_sync_fetch = False


def pr_keys(text: str) -> list[tuple[str, str, int]]:
    """GitHub PR references in ``text``, in order, deduped."""
    out: list[tuple[str, str, int]] = []
    for m in _PR_URL.finditer(text or ""):
        key = (m.group(1).lower(), m.group(2).lower(), int(m.group(3)))
        if key not in out:
            out.append(key)
    return out[:_MAX_PRS_PER_MESSAGE]


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return None


def _reviewer_name(node: Any) -> str:
    node = node if isinstance(node, dict) else {}
    name = node.get("login") or node.get("slug") or ""
    return f"{node.get('__typename', '')}:{name}".lower()


def parse_pr(data: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a GraphQL response to the fields the stripe needs."""
    d = (data or {}).get("data") or {}
    viewer = str(((d.get("viewer") or {}).get("login")) or "").lower()
    pr = ((d.get("repository") or {}).get("pullRequest")) or None
    if not pr:
        return None
    requested = [
        _reviewer_name((n or {}).get("requestedReviewer"))
        for n in ((pr.get("reviewRequests") or {}).get("nodes") or [])
    ]
    events: dict[str, float] = {}
    for n in ((pr.get("timelineItems") or {}).get("nodes") or []):
        name = _reviewer_name((n or {}).get("requestedReviewer"))
        at = _ts((n or {}).get("createdAt"))
        if at is not None:
            events[name] = max(at, events.get(name, 0.0))
    requested_at = [events[r] for r in requested if r in events]
    return {
        "open": pr.get("state") == "OPEN" and not pr.get("isDraft"),
        "review_required": pr.get("reviewDecision") == "REVIEW_REQUIRED",
        "viewer": viewer,
        "author": str(((pr.get("author") or {}).get("login")) or "").lower(),
        "requested": [r for r in requested if r],
        "requested_at": max(requested_at) if requested_at else _ts(pr.get("createdAt")),
    }


def review_state(info: dict[str, Any] | None, now: float | None = None) -> str | None:
    """``"stuck"`` / ``"parked"`` / None for one PR (see module doc)."""
    if not info or not info.get("open") or not info.get("review_required"):
        return None
    viewer = info.get("viewer") or ""
    me = f"user:{viewer}"
    requested = info.get("requested") or []
    if viewer and me in requested:
        return "stuck"
    if not viewer or info.get("author") != viewer:
        return None  # someone else's PR that isn't asking me: not this session's wait
    if not requested:
        return "stuck"
    since = info.get("requested_at")
    now = time.time() if now is None else now
    if since is not None and now - since < PARK_WINDOW_S:
        return "parked"
    return "stuck"


def _gh() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if os.access(cand, os.X_OK):
            return cand
    return None


def _fetch_pr(key: tuple[str, str, int]) -> dict[str, Any] | None:
    if _fetch_override is not None:
        return _fetch_override(key)
    gh = _gh()
    if not gh:
        return None
    owner, repo, number = key
    proc = subprocess.run(
        [gh, "api", "graphql", "-F", f"o={owner}", "-F", f"r={repo}", "-F", f"n={number}",
         "-f", f"query={_QUERY}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, stdin=subprocess.DEVNULL, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200])
    return parse_pr(json.loads(proc.stdout))


def _refresh(key: tuple[str, str, int]) -> None:
    try:
        info, ttl = _fetch_pr(key), _PR_TTL_S
    except Exception as exc:
        _log.debug("review lookup %s failed: %s", key, exc)
        info, ttl = None, _PR_ERROR_TTL_S
    with _lock:
        _pr_cache[key] = (time.monotonic() + ttl, info)
        _inflight.discard(key)
        listeners = tuple(_refresh_listeners)
    for listener in listeners:
        listener()


def _pr_info(key: tuple[str, str, int]) -> dict[str, Any] | None:
    """Cached PR info; schedules a refresh when missing or stale."""
    with _lock:
        hit = _pr_cache.get(key)
        stale = hit is None or time.monotonic() >= hit[0]
        start = stale and key not in _inflight
        if start:
            _inflight.add(key)
    if start:
        if _sync_fetch:
            _refresh(key)
            with _lock:
                hit = _pr_cache.get(key)
        else:
            threading.Thread(target=_refresh, args=(key,), daemon=True,
                             name="review-wait-lookup").start()
    return hit[1] if hit else None


def _db_path(profile: Any) -> Path | None:
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home

        name = str(profile or "").strip()
        if name and name != "default":
            p = Path(get_default_hermes_root()) / "profiles" / name / "state.db"
        elif name == "default":
            p = Path(get_default_hermes_root()) / "state.db"
        else:
            p = Path(get_hermes_home()) / "state.db"
        return p if p.is_file() else None
    except Exception:
        return None


def _row_ids(row: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.append(str(sid))
    return seen


def _last_assistant_text(conn: sqlite3.Connection, ids: list[str]) -> str:
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"select content from messages where session_id in ({marks}) and role='assistant'"
        " and coalesce(content,'') <> '' order by id desc limit 1",
        ids,
    )
    hit = cur.fetchone()
    return str(hit[0]) if hit and hit[0] else ""


def _row_prs(rows: Iterable[dict[str, Any]]) -> dict[int, list[tuple[str, str, int]]]:
    """PR keys per row (by list position), reading only rows that changed."""
    out: dict[int, list[tuple[str, str, int]]] = {}
    pending: dict[Path, list[tuple[int, dict[str, Any], tuple[Any, Any]]]] = {}
    for i, row in enumerate(rows):
        rid = str(row.get("id") or "")
        if not rid:
            continue
        sig = (row.get("last_active"), row.get("message_count"))
        with _lock:
            hit = _msg_cache.get(rid)
        if hit and hit[0] == sig:
            out[i] = hit[1]
            continue
        path = _db_path(row.get("profile"))
        if path is not None:
            pending.setdefault(path, []).append((i, row, sig))
    for path, items in pending.items():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        except Exception:
            continue
        try:
            for i, row, sig in items:
                try:
                    keys = pr_keys(_last_assistant_text(conn, _row_ids(row)))
                except Exception:
                    continue
                with _lock:
                    _msg_cache[str(row["id"])] = (sig, keys)
                out[i] = keys
        finally:
            conn.close()
    return out


@_fact_consumer('review')
def annotate_review_wait(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``review_wait`` (``"stuck"`` / ``"parked"``) and ``review_wait_pr``
    on rows waiting on a code review; ``stuck`` wins across several PRs."""
    if not rows:
        return rows
    try:
        prs = _row_prs(rows)
        now = time.time()
        for i, row in enumerate(rows):
            reviews = [(key, _pr_info(key)) for key in prs.get(i, ())]
            project_review_row(row, reviews=reviews, now=now)
    except Exception:  # health: allow BLE001 -- legacy best-effort reader boundary; preserves rows on provider failure
        _log.debug("review wait annotation failed", exc_info=True)
    return rows


def project_review_row(row, *, reviews, now):
    """Pure serializer over ordered ((owner, repo, number), captured PR info) pairs."""
    best: tuple[str, str] | None = None
    for key, info in reviews:
        state = review_state(info, now)
        if state and (best is None or (state == "stuck" and best[0] != "stuck")):
            best = (state, f"https://github.com/{key[0]}/{key[1]}/pull/{key[2]}")
    if best:
        row["review_wait"], row["review_wait_pr"] = best
    else:
        row.pop("review_wait", None)
        row.pop("review_wait_pr", None)
    return row


def _reset_cache_for_tests() -> None:
    with _lock:
        _pr_cache.clear()
        _inflight.clear()
        _msg_cache.clear()
