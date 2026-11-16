"""One card per host when this host cannot reach the Board, instead of one per board. [daveey]

Incident (2026-11-15, David-MBP, t_01a1e1174026210f): DNS resolution of the Board failed from 13:25 to 13:50 PST
(``URLError: [Errno 8] nodename nor servname provided``) on every synced board at once, and the replica-freshness
alarm (``kanban_sync_replica_age``) opened ten board-specific stale-replica cards for that one cause. Every board
syncs against the same Board URL with the same identity, so a failed name lookup or connection is a fact about this
host's path to the Board, not about any board.

:func:`classify` names that class from the round's exception object (never its text): ``dns`` for a failed name
lookup (``socket.gaierror``), ``network`` for a refused/reset/aborted connection, a timeout or an unreachable
network or host. Anything else (an HTTP status, SQLite, a resync, a missing round) stays unclassified and keeps its
own per-board card. The replica check gathers every board that comes due with a classified cause from a recent
round into one host incident (:func:`report`): one card listing each affected board with its detail and the cards
frozen behind it; a board that joins later is named in an unthrottled comment. The incident ends when none of its
boards' replica incidents is open; the next one is fresh (a new card once the old one is done, else a "new
incident" comment). This alarm does not diagnose why the resolver or network failed.
"""
from __future__ import annotations

import errno
import logging
import socket
import urllib.error
from typing import Optional

log = logging.getLogger(__name__)

SLUG = "_host-network"  # the host card's idempotency suffix: never a board id (BOARD_ID_RE starts alphanumeric)
_NET_ERRNOS = frozenset(getattr(errno, n) for n in (
    "ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN", "ECONNREFUSED", "ECONNRESET", "ECONNABORTED",
    "ETIMEDOUT", "ENETRESET") if hasattr(errno, n))


def classify(exc: Optional[BaseException]) -> Optional[str]:
    """``dns``, ``network`` or None (not a host-to-Board transport failure), walking ``URLError.reason`` and the
    exception chain."""
    seen: set[int] = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return "dns"
        if isinstance(cur, (ConnectionError, TimeoutError, socket.timeout)):
            return "network"
        if isinstance(cur, OSError) and not isinstance(cur, urllib.error.URLError) and cur.errno in _NET_ERRNOS:
            return "network"
        reason = getattr(cur, "reason", None) if isinstance(cur, urllib.error.URLError) else None
        cur = reason if isinstance(reason, BaseException) else (cur.__cause__ or cur.__context__)
    return None


def report(entries: list[tuple[str, str, str]], *, fresh: bool, cause: str) -> Optional[str]:
    """Open the host incident's card, or comment the open one. ``entries``: ``(board, detail, frozen text)`` for
    the boards new to the incident; ``fresh``: they start a new incident. Best effort: never raises."""
    from hermes_cli import kanban_sync_outbox_age as outbox_age
    from hermes_cli import kanban_sync_replica_age as replica_age
    host = socket.gethostname()
    boards = ", ".join(f"`{b}`" for b, _d, _f in entries)
    detail = f"host {host}: cannot reach the Board ({cause}); replica stale on {boards}"
    sections = "\n\n".join(f"### `{b}`\n{d}\n\n{f}" for b, d, f in entries)
    body = (
        f"This host could not reach the Board ({cause} failure on the sync rounds), so {len(entries)} synced "
        "board replica(s) have not reached the feed head for over 20 awake minutes. One shared cause, one card: "
        "every affected board and the cards frozen behind it are listed here (and in later comments), instead of "
        "one card per board. Remote parents, missing-parent checks and ready notices read an old copy meanwhile "
        "(they wait; nothing promotes on stale data). Pending local edits stay in each outbox and are sent once "
        "the Board is reachable; the sync loop backs failed rounds off up to 60 s.\n\n"
        f"{sections}\n\n"
        "This alarm classifies the failure from the round's exception only; it does not know why name resolution "
        "or the network failed. Check: `hermes kanban sync status`, agent.log `kanban sync: round for` lines, and "
        "the host's resolver/network state. Done when every listed replica reaches the head again and the cause "
        "is understood (precedent: t_01a1e1174026210f)."
    )
    comment = (("New incident: " if fresh else "Also affected: ") + detail + "\n\n" + sections)
    return outbox_age.report(SLUG, detail, key=replica_age.KEY, author=replica_age.AUTHOR, body=body,
                             what="Board unreachable", comment=comment,
                             title=f"Kanban sync: {host} cannot reach the Board ({cause}); "
                                   f"{len(entries)} board replica(s) stale 20+ awake min")
