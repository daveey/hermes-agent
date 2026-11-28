"""Keep the links projection across commits to local boards it never read. [daveey]

The remote-links projection (``kanban_session_activity._store_links``) reads a local board only for this origin's own
receipt rows, ``(gateway, profile) == (origin_gateway, origin_profile)``: ``project()``'s ``observe_local_binding``
and ``_without_local_open_duplicates`` both read exactly that row's ``board``. Its fingerprint
(``_links_fingerprint``) still covers every board, so on hermes-hub a commit on any of ~14 boards re-projected the
ledger and the hub replica on the next 3 s poll (t_01a1cee98aaa8db1).

``remember`` records, for a published entry, the boards its origin rows referenced. ``matches`` then accepts a
current fingerprint that differs from the entry's only in the content keys of boards the entry never read. The hub
replica, the ledger and every referenced board must be equal, and a board path the entry did not know (a new board)
always differs, so read-your-writes holds for every input the answer depends on. Only a projection that declares
this read pattern (``DECLARES_BOARD_READS`` set on the function) is narrowed; a row with no readable identity
records nothing, and the entry is then served by its exact fingerprint only. The entry's own max age is unchanged.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable

DECLARES_BOARD_READS = "declares_board_reads"

_lock = threading.Lock()
# scope -> (entry, board paths its rows read, every board path known when it was published)
_read: dict[tuple, tuple[tuple, frozenset[str], frozenset[str]]] = {}


def remember(entry: tuple, project: Callable, board_paths: Iterable[Any], slug_of: Callable[[Any], str], *,
             local_boards: Iterable[str] = ()) -> tuple:
    """Record which local boards ``entry`` (``(expires, scope, fingerprint, cards, origin rows)``) read; returns it."""
    with _lock:
        _read.pop(entry[1], None)
    if not getattr(project, DECLARES_BOARD_READS, False):
        return entry
    paths: dict[str, set[str]] = {}
    for p in board_paths:  # a slug two boards share (boards/default vs kanban.db) counts as read on both
        paths.setdefault(slug_of(p), set()).add(str(p))
    # Legacy duplicates can disappear from the output; retain their read custody
    # separately without adding non-ledger rows to the public origin inventory.
    read: set[str] = set(local_boards)
    for row in entry[4]:
        try:
            local = (row["gateway"], row["profile"]) == (row["origin_gateway"], row["origin_profile"])
            board = row["board"]
        except (KeyError, TypeError):
            return entry  # unknown shape: exact fingerprint only
        if local and board in paths:
            read.update(paths[board])
    with _lock:
        _read.pop(entry[1], None)
        _read[entry[1]] = (entry, frozenset(read), frozenset(p for ps in paths.values() for p in ps))
        while len(_read) > 64:
            del _read[next(iter(_read))]
    return entry


def _narrowed(fingerprint: tuple, boards: frozenset[str], read: frozenset[str]) -> tuple:
    return tuple(e for e in fingerprint
                 if not (isinstance(e, tuple) and len(e) == 2 and e[0] in boards and e[0] not in read))


def matches(hit: tuple, fingerprint: tuple) -> bool:
    """``hit`` answers for ``fingerprint``: equal except in the keys of known boards it never read."""
    with _lock:
        info = _read.get(hit[1])
    if info is None or info[0] is not hit:
        return False
    _, read, boards = info
    return _narrowed(hit[2], boards, read) == _narrowed(fingerprint, boards, read)


def reset() -> None:
    with _lock:
        _read.clear()
