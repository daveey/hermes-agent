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

import threading
import time
from pathlib import Path
from typing import Any, Iterable

PULL_PAGE_BUDGET = 20  # 20 pages x 500 changes = 10k changes per database per round
BUSY_RETRY_S = 0.5  # a round skipped because a dispatcher tick held the board (wait_for_work's tick)
RECONCILE_BUSY_WAIT_S = 600.0  # a reconcile waits this long in all for rounds holding the board between its chunks
_sleep = time.sleep  # tests

_LOCK = threading.Lock()
_BEHIND: set[str] = set()  # live board paths whose last round stopped before the feed head


class Pulled(int):
    """The number of feed changes read (an ``int``, as before), plus whether the pull stopped at its page budget
    with the head not reached (``more``) or did not run because a sync round held the board (``busy``)."""

    more: bool
    busy: bool

    def __new__(cls, n: int = 0, *, more: bool = False, busy: bool = False) -> Pulled:
        out = super().__new__(cls, int(n))
        out.more, out.busy = bool(more), bool(busy)
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
        if not pulled.busy and not pulled.more:
            return
        if pulled.busy:
            if waited >= RECONCILE_BUSY_WAIT_S:
                raise SyncRoundBusy("a kanban sync round keeps holding this board; rerun the reconcile later")
            _sleep(BUSY_RETRY_S)
            waited += BUSY_RETRY_S


def reset() -> None:
    """Tests: forget every board."""
    with _LOCK:
        _BEHIND.clear()
