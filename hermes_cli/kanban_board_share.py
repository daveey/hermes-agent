"""Shared host slots across boards: one priority order for every board, with a starvation guard. [daveey]

``kanban.max_in_progress`` caps the HOST: each board's dispatcher tick counts the running workers on every other
board against it (``kanban_db_dispatch._tick_spawn_budget``). The gateway ticks boards one after another in a
rotating order (``_KanbanDispatcher._fair_board_order``) and each tick used to take every free slot for its own
cards, so a freed slot went to whichever board's turn came first, not to the best card. On hermes-hub
2026-11-12 board ``hermes`` started two priority-0 cards (05:08:50/52) while default-board t_01a1ac912db0cb5c
(priority 51010) sat ready from 04:40 reading "0 card(s) ahead of it, 9/9 worker slots busy"; chats raised it to
60320, which could not help (effective priority is capped at ``kanban_priority.PRIORITY_CAP``, 1000, anyway).

When the free slots are fewer than the dispatchable cards on all boards, :func:`shares` ranks every board's cards
in the dispatcher's own order and gives each board a spawn quota: how many of the top ``free`` cards are its own.
:func:`run` ticks the boards with a quota first and hands a quota a board could not spend (a claim refused at spawn
time) to the next board. Without contention it returns None and every board ticks as before.

The order is the dispatcher's aged admission score (``kanban_admission_score.rank``: effective priority, class,
cards waiting on it, minutes waiting, failures), computed over every eligible card before the per-board cut, so a
long-waiting low-priority card on another board outranks fresh higher-priority work after (priority gap / age
weight) minutes instead of only through the starvation guard (t_01a1dca84c849edc). One difference: across boards a
card's age counts from the later of its ready/review entry and its board's latest worker start. A board that keeps
starting cards is being served, and its own tick spends a quota in its own aged order; without the floor, a head
the claim loop keeps skipping (respawn guard, per-profile cap, pacing) would age into every tick's first pick and
hand its board's slots to lower cards there. Its board wins again only once it has started nothing for that long.

Starvation guard (deliberate per-board minimum, kept alongside aging): priorities are set per board by different
owners, so a board whose top card has been ready :data:`STARVE_SECONDS` or longer, while the board started no card,
takes the first pick (oldest wait first) whatever the scores, so no board waits much longer than that plus one
worker turnover, and a head that can never start wins its board at most one starvation pick per window. Ranking is
advisory: guards and refusals decided at claim time are not modelled, and the host cap check in each board's tick
stays authoritative.
"""
from __future__ import annotations

import contextlib
import heapq
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

_log = logging.getLogger(__name__)

STARVE_SECONDS = 2 * 60 * 60
OTHER_BOARDS = "other boards sharing these slots"


class Candidate(NamedTuple):
    task_id: str
    priority: int
    waited_on: bool
    created_at: float
    ready_since: float  # the starvation clock, set on a board's head only (:func:`_starve_clock`)
    score: Optional[float] = None  # cross-board aged admission score; None: raw effective priority (fallback)
    since: float = 0.0  # the score's age clock: its first tie-break, as in ``kanban_admission_score.rank``
    turn_owner: bool = False  # holds a land turn/lane: ahead of every non-owner (``kanban_admission_score.rank``)

    def rank(self) -> tuple:
        if self.score is None:
            return (True, -self.priority, self.since, not self.waited_on, self.created_at, self.task_id)
        return (not self.turn_owner, -self.score, self.since, False, self.created_at, self.task_id)


def candidates(conn, limit: int, *, now: Optional[float] = None) -> list[Candidate]:
    """Up to ``limit`` unclaimed ready (and review, when review dispatch is on) cards this host could start, best
    first; cards this gateway cannot lease (``dispatch_refusals``) are left out. Every eligible card is scored
    before the cut, so an aged card below a priority-ordered ``LIMIT`` is not dropped unseen."""
    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled
    from hermes_cli.kanban_db_dispatch_queries import _WAITERS, DISPATCH_ORDER, EFFECTIVE_PRIORITY
    from hermes_cli.kanban_worker_node import dispatch_refusals
    now = time.time() if now is None else now
    rows: list = []
    for status in ("ready", "review") if review_dispatch_enabled() else ("ready",):
        refused = dispatch_refusals(conn, status)
        rows += [r for r in conn.execute(
            f"SELECT id, created_at, {EFFECTIVE_PRIORITY} AS eff, EXISTS (SELECT 1 {_WAITERS}) AS waited "
            f"FROM tasks WHERE status = ? AND claim_lock IS NULL ORDER BY {DISPATCH_ORDER}", (status,)).fetchall()
                 if r["id"] not in refused]
    found = sorted(_scored(conn, rows, now), key=Candidate.rank)[:limit]
    if found:  # only the head's wait decides starvation; one event scan per board
        found[0] = found[0]._replace(ready_since=_starve_clock(conn, found[0]))
    return found


def _scored(conn, rows: list, now: float) -> list[Candidate]:
    """``rows`` as candidates scored by the dispatcher's aged admission score, with the age component counted
    from the later of the card's ready/review entry and the board's latest worker start (see the module
    docstring). Raw effective priority when the score cannot be read (the dispatcher's own fallback)."""
    from hermes_cli.kanban_admission_score import AGE_BUCKET_S, rank
    plain = [Candidate(r["id"], int(r["eff"] or 0), bool(r["waited"]), float(r["created_at"] or 0), 0.0)
             for r in rows]
    try:
        scored = rank(conn, rows, now_ms=int(now * 1000))
    except (sqlite3.Error, OSError, ValueError):
        _log.warning("kanban dispatcher: cross-board admission score unavailable; ranking by priority",
                     exc_info=True)
        return plain
    served = _served_at(conn)
    by_id = {c.task_id: c for c in plain}
    out = []
    for row, parts in scored:
        since = max(parts.ready_since, served)
        age = dict(parts.weights)["age"]
        score = parts.total - age * (parts.age_buckets - max(0, int(now - since)) // AGE_BUCKET_S)
        out.append(by_id[row["id"]]._replace(priority=parts.effective_priority, score=score, since=float(since),
                                             turn_owner=parts.turn_owner))
    return out


def _served_at(conn) -> int:
    """The board's latest worker start (0 if none): the floor of its cards' cross-board age."""
    row = conn.execute("SELECT MAX(started_at) FROM task_runs").fetchone()
    return int(row[0] or 0) if row is not None else 0


def _starve_clock(conn, head: Candidate) -> float:
    """When the board's starvation wait began: the head's last entry into ready, or the board's latest worker
    start if later. A board that starts any card is served, so a head the claim loop keeps skipping (dispatch
    guard, per-profile cap, claim deferral, no assignee) earns its board at most one first pick per
    :data:`STARVE_SECONDS` instead of the first pick on every tick."""
    from hermes_cli.kanban_parent_unclaimed import ready_since
    since = float(ready_since(conn, head.task_id, int(head.created_at)))
    return max(since, float(_served_at(conn)))


def _starved(cands: list[Candidate], now: float) -> bool:
    return bool(cands) and cands[0].ready_since > 0 and now - cands[0].ready_since >= STARVE_SECONDS


def plan(heads: dict[str, list[Candidate]], free: Optional[int], *, now: float) -> Optional[dict[str, int]]:
    """``{board: quota}`` in first-pick order for ``free`` shared slots, or None when every candidate fits (or the
    host is uncapped / full: each board's own cap check decides). Boards without a pick are absent."""
    if free is None or free <= 0 or sum(len(c) for c in heads.values()) <= free:
        return None
    queues = {slug: list(c) for slug, c in heads.items() if c}
    picks: list[str] = []
    for _since, slug in sorted((q[0].ready_since, slug) for slug, q in queues.items() if _starved(q, now)):
        if len(picks) < free:
            queues[slug].pop(0)
            picks.append(slug)
    ranked = heapq.merge(*([(c.rank(), slug) for c in q] for slug, q in queues.items()))
    for _rank, slug in ranked:
        if len(picks) >= free:
            break
        picks.append(slug)
    quotas: dict[str, int] = {}
    for slug in picks:
        quotas[slug] = quotas.get(slug, 0) + 1
    return quotas


@contextlib.contextmanager
def _board_conn(slug: str):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kb.pin_first_board_resolution():
        conn = kbc.connect(board=slug)
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _db_path(conn) -> Optional[str]:
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(Path(row[2]).resolve()) if row is not None and row[2] else None


def shares(slugs: list[str], cap: Optional[int], *, now: Optional[float] = None) -> Optional[dict[str, int]]:
    """:func:`plan` for this tick's boards under host cap ``cap``; None (tick as before) for one board, no cap,
    boards sharing one DB file, or any read error."""
    if cap is None or len(slugs) < 2:
        return None
    from hermes_cli.kanban_db_dispatch import count_running_tasks
    now = time.time() if now is None else now
    heads: dict[str, list[Candidate]] = {}
    paths: set[str] = set()
    running = 0
    for slug in slugs:
        try:
            with _board_conn(slug) as conn:
                path = _db_path(conn)
                if path is None or path in paths:  # HERMES_KANBAN_DB pins every slug to one file
                    return None
                paths.add(path)
                running += count_running_tasks(conn)
                heads[slug] = candidates(conn, cap, now=now)
        except Exception:  # health: allow BLE001 -- an unreadable (e.g. quarantined) board only gets unspent quota
            _log.debug("kanban dispatcher: board %s unreadable for cross-board slot ranking", slug, exc_info=True)
    return plan(heads, cap - running, now=now)


_tick = threading.local()


@contextlib.contextmanager
def tick_counts():
    """One dispatcher tick over every board: :func:`count_running_tasks_other_boards` reads each board once.

    Every board's tick asked every other board for its running count: 12 boards meant 132 connections per tick
    (5% of hub gateway CPU, 2026-11-12). Inside this scope a board's count is kept until that board's own tick
    asks (its tick is what starts workers there), so the next board reads it again and still sees those starts.
    A worker that ends on another board mid-tick is counted until the next tick, which only delays a start.
    Outside the scope (daemon, CLI, notices) every call reads every board, as before. [daveey]"""
    previous = getattr(_tick, "counts", None)
    _tick.counts = {}
    try:
        yield
    finally:
        _tick.counts = previous


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board`` (re-exported by ``kanban_db_dispatch``).

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every
    board to one file) yields 0. Fails open per board.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli.kanban_db_dispatch import count_running_tasks
    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current_path = None
    # Without the asking board's path nothing says whose count goes stale: read every board.
    counts: Optional[dict[str, int]] = getattr(_tick, "counts", None) if current_path is not None else None
    if counts is not None:
        counts.pop(current_path, None)  # this board's tick is about to change its own count
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return 0
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if counts is not None and resolved in counts:
                total += counts[resolved]
                continue
            if not path.exists():
                continue
            other = _kbc.connect(board=slug)
            try:
                running = count_running_tasks(other)
            finally:
                with contextlib.suppress(Exception):
                    other.close()
            total += running
            if counts is not None:
                counts[resolved] = running
        except Exception:
            continue
    return total


def run(order: list[str], quotas: dict[str, int], tick: Callable[..., Any]) -> list[tuple[str, Any]]:
    """Tick boards with a quota first (first-pick order), then the rest; an unspent quota passes on."""
    out: list[tuple[str, Any]] = []
    spare = 0
    for slug in [*quotas, *(s for s in order if s not in quotas)]:
        quota = quotas.get(slug, 0) + spare
        res = tick(slug, spawn_quota=quota)
        spare = max(0, quota - len(getattr(res, "spawned", None) or []))
        out.append((slug, res))
    return out


def ahead_elsewhere(conn, task_id: str, *, now: Optional[float] = None, limit: int = 500) -> int:
    """Cards on OTHER boards that rank ahead of ready ``task_id`` for the shared slots (any assignee: the cap is
    the host's), a starved board's head included. 0 when the card is not a candidate or no other board exists."""
    from hermes_cli import kanban_db as kb
    now = time.time() if now is None else now
    mine = candidates(conn, limit, now=now)
    own = next((c for c in mine if c.task_id == task_id), None)
    if own is None:
        return 0
    own_starved = mine[0] is own and _starved(mine, now)  # it takes a first pick; only older starved heads lead
    own_path = _db_path(conn)
    total = 0
    for meta in kb.list_boards(include_archived=False):
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        try:
            with _board_conn(slug) as other:
                if _db_path(other) == own_path:
                    continue
                cands = candidates(other, limit, now=now)
        except Exception:  # health: allow BLE001 -- an unreadable board adds nothing to an advisory count
            _log.debug("kanban queue position: board %s unreadable", slug, exc_info=True)
            continue
        if own_starved:
            total += int(_starved(cands, now) and cands[0].ready_since < own.ready_since)
            continue
        ahead = [c for c in cands if c.rank() < own.rank()]
        if _starved(cands, now) and cands[0] not in ahead:
            ahead.append(cands[0])
        total += len(ahead)
    return total
