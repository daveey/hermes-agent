"""A bounded feed pull per sync round, and the boards whose backlog the loop drains next. [daveey]

Incident (2026-11-12, David-MBP, t_01a1d36ddcea59fe): after the Mac slept ~10 h the live cursor was ~75k changes
behind. ``pull()`` read pages until the feed said ``more=false``, and a round pushed its outbox only after its pulls,
so on a host that applies ~4 changes/s against a Board producing ~2.3/s the round never reached its push: 1747 local
edits (oldest 9 h) sat in the outbox with ``attempts=0`` while the Board kept MacBook cards ``scheduled``.

Now a round pushes before it pulls (``_sync_board_locked``), each ``pull`` reads at most :data:`PULL_PAGE_BUDGET`
pages per database, and a round that stops with ``more`` still true is recorded here so the sync loop reselects
that board at once, without the poll wait: the backlog drains over consecutive rounds, each of which pushes.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)
PULL_PAGE_BUDGET = 20  # 20 pages x 500 changes = 10k changes per database per round
BUSY_RETRY_S = 0.5  # a round skipped because a dispatcher tick held the board (wait_for_work's tick)
RECONCILE_BUSY_WAIT_S = 600.0  # a reconcile waits this long in all for rounds holding the board between its chunks
_sleep = time.sleep  # tests


def sendable_before_head(conn: Any, hub: Any, board: str, rows: list[dict], seen: dict) -> tuple[list[dict], bool]:
    """The outbox rows a push may send while this file has not pulled to the feed head (``push(before_head=True)``),
    and whether it stopped: the first run row ends the batch (FIFO kept: a run row is sent whole and the Board keeps
    the last write, so it waits for any end in an unread page). A task row with a live ``status`` whose Board row is
    done/archived (``kanban_terminal_guard.board_may_hold_terminal``) is skipped with that card's later ``tasks``
    rows (the key is the table and row uid); other rows still go, so one stale wake holds back no one else's edits.
    The outbox-age alarm applies this same rule (``kanban_sync_outbox_age``), so it never ages a row held here."""
    from hermes_cli.kanban_terminal_guard import board_may_hold_terminal
    out: list[dict] = []
    skipped: set[tuple[str, str]] = set()
    for r in rows:
        if r["table_name"] == "task_runs":
            return out, True
        key = (r["table_name"], r["row_uid"])
        if key in skipped or board_may_hold_terminal(conn, hub, board, r, seen):
            skipped.add(key)
            continue
        out.append(r)
    return out, False


_LOCK = threading.Lock()
_BEHIND: set[str] = set()  # live board paths whose last round stopped before the feed head
_EVIDENCE: dict[str, dict] = {}  # live board path -> its latest round's Board reads ({card id: may hold terminal})


def round_evidence(path: Path) -> dict:
    """A fresh Board-read cache for this round, shared by its sends before and after the pulls, so a card is read
    at most once per round (``board_may_hold_terminal``). The outbox-age alarm reads the latest one back."""
    seen: dict = {}
    with _LOCK:
        _EVIDENCE[str(path)] = seen
    return seen


def evidence(path: Path) -> dict:
    """A copy of the latest round's Board reads for this board (empty before any round)."""
    with _LOCK:
        return dict(_EVIDENCE.get(str(path)) or {})


class Pulled(int):
    """The number of feed changes read (an ``int``, as before), plus whether the pull stopped at its page budget
    with the head not reached (``more``) or did not run because a sync round held the board (``busy``), and
    ``status_changed`` ({card id: new status}: every status change the pull applied, last one wins)."""

    more: bool
    busy: bool
    status_changed: dict

    def __new__(cls, n: int = 0, *, more: bool = False, busy: bool = False,
                status_changed: Any = None) -> Pulled:
        out = super().__new__(cls, int(n))
        out.more, out.busy = bool(more), bool(busy)
        out.status_changed = dict(status_changed or {})
        return out


def note(path: Path, more: bool) -> None:
    """Record whether this round left the board behind the feed head."""
    key = str(path)
    with _LOCK:
        if more:
            _BEHIND.add(key)
        else:
            _BEHIND.discard(key)


def due(boards: Iterable[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """The boards to run again now: their last round stopped at the page budget."""
    with _LOCK:
        return [(slug, path) for slug, path in boards if str(path) in _BEHIND]


def behind(path: Path) -> bool:
    """The board's last round stopped before the feed head (its run rows are held back meanwhile)."""
    with _LOCK:
        return str(path) in _BEHIND


def replica_at_head(slug: str, hub: Any, gateway: str, live_path: Path) -> None:
    """Pull the replica to the feed head for ``reconcile`` (it reads tombstones from it), :data:`PULL_PAGE_BUDGET`
    pages at a time with the board lock released between chunks: under a backlog one unbounded locked pull would
    refuse every sync round, and so every push, for the whole catch-up. A round holding the board pauses the chunks
    (it pulls the same replica forward); over :data:`RECONCILE_BUSY_WAIT_S` of such waits raises SyncRoundBusy."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_sync_guard import SyncRoundBusy
    waited = 0.0
    while True:
        pulled = ksc._pull_replica(slug, hub, gateway, live_path)
        if pulled.status_changed:  # a park naming a card this pull moved re-probes now, as after a round [daveey]
            _expedite(live_path, pulled.status_changed)
        if not pulled.busy and not pulled.more:
            return
        if pulled.busy:
            if waited >= RECONCILE_BUSY_WAIT_S:
                raise SyncRoundBusy("a kanban sync round keeps holding this board; rerun the reconcile later")
            _sleep(BUSY_RETRY_S)
            waited += BUSY_RETRY_S


def pull_live(live: sqlite3.Connection, hub: Any, slug: str, gateway: str) -> bool | None:
    """Pull a primary board's live file (``gateway``'s partition): True while it is still behind the feed head, None
    when a cold file was rebuilt from a snapshot instead (the caller's repair treats it as a rebuild).

    A live file that has never pulled (cursor 0) would replay every change the board ever had, at
    :data:`PULL_PAGE_BUDGET` pages per round, and every round of that replay ends behind the head, so its sends run
    ``before_head`` and hold the outbox at its first run row. Incident (autoscaled worker-a1, 2026-11-13,
    t_01a1d65b80d2e9f4): the enroll cold-started the replica from a snapshot, the live file began at 0 against a
    ~1.93M-change feed (cursor 1586950 after 17 min) and a claimed run's 24 ops never left with ``attempts=0``,
    including the requeue that ends the Board lease. So a cold file whose first page is not the head rebuilds from
    the partitioned snapshot, as on a 410, and resumes the feed at its head. Upserts only (``resync``), no absent-row
    sweep: a just-attached board's local cards are not on the Board yet and are queued by ``import``, not deleted.

    The first page is only peeked, never applied: the cursor stays 0 until ``resync`` writes the head, so a rebuild
    cut short (a snapshot HubError, a stop) is retried as a rebuild next round instead of leaving a warm-looking file
    that falls back to the full replay (review of PR #2657).
    """
    from hermes_cli import kanban_awaited_card_wake as acw
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    cold = not int((sync.sync_state(live) or {}).get("cursor") or 0)
    if cold and acw.changes_page(hub, slug, 0, {}).get("more"):
        _log.warning("kanban sync: %s live file never pulled and the feed is past one page; rebuilding it from a "
                     "snapshot instead of replaying the board's history", slug)
        ksc.resync(live, hub, slug, observers=True, partition=gateway)
        return None
    with ksc.credited(live):  # the client's name: tests replace it there
        return ksc.pull(live, hub, slug, observers=True, partition=gateway, max_pages=PULL_PAGE_BUDGET).more


def _expedite(live_path: Path, changed: dict) -> None:
    """``kanban_awaited_card_wake.expedite`` on the live board for a standalone replica pull (``reconcile``): the
    sync round's own call never sees these changes, since the replica cursor has moved past them. Never raises."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_awaited_card_wake import expedite
    path = Path(live_path)
    try:
        with ksc.connection(path, lambda: ksc._connect(path)) as live:
            expedite(live, changed)
    except (sqlite3.Error, OSError) as exc:
        _log.warning("kanban sync: %s: parks naming %d moved card(s) not re-probed (%s)", path, len(changed), exc)


def reset() -> None:
    """Tests: forget every board."""
    with _LOCK:
        _BEHIND.clear()
        _EVIDENCE.clear()
