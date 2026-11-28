"""The JSON-RPC ``projects.tree`` store reads, kept until the store commits. [daveey]

The all-profiles HTTP tree already reuses each store's session page per commit
(``web_routers.profiles._PROJECT_TREE_SCAN_CACHE``). The Desktop's own-profile RPC did not: every
``projects.tree`` re-read the 2,000-row page and aggregated ``distinct_session_cwds`` over the whole
``sessions`` table, though the store had not changed. On the hub the tui-rpc pool spent ~15k
voluntary context switches/s in that loop (t_01a20286f9f3f262). Both reads are pure projections of
``state.db`` (plus config, in the key), so they are kept until the store's write generation moves,
checked once per request; there is no TTL and no timer. Annotation and the tree build still run.

The change signal is the SessionDB's own ``_write_generation()`` (its writer connection, that
connection's ``total_changes`` and ``PRAGMA data_version``): no connection is opened or retained
here, so profile deletion's resource custody is unchanged. The kept rows live on the SessionDB
object (weakly keyed) and go with it; a store without a writer connection is never reused.
"""
from __future__ import annotations

import threading
import weakref
from typing import Any, Callable

_LOCK = threading.Lock()
_KEPT: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # db -> {key: (stamp, rows)}
_MAX_KEYS = 16


def _stamp(db) -> tuple | None:
    """A comparable write generation of ``db``, or None when it has none (then nothing is reused)."""
    generation = getattr(db, "_write_generation", None)
    if generation is None:
        return None
    try:
        value = generation()
    except Exception:  # health: allow BLE001 -- an unreadable generation only disables reuse
        return None
    if not (isinstance(value, tuple) and len(value) == 3):
        return None  # a closed store: equal to nothing
    conn, changes, version = value
    try:
        ref = weakref.ref(conn)
    except TypeError:
        return None
    return ref, changes, version


def _same(a: tuple, b: tuple) -> bool:
    return a[0]() is not None and a[0]() is b[0]() and a[1:] == b[1:]


def _clone_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fresh row dicts for one response: annotation and tree building stamp them; their container values
    (``_lineage_ids``) are copied too, so nothing a caller mutates reaches the kept copy."""
    return [{k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict)
                 else set(v) if isinstance(v, set) else v) for k, v in row.items()} for row in rows]


def read(db, key: tuple, scan: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """``scan()``, or a copy of its last result while ``db`` has not committed (any connection) since."""
    before = _stamp(db)
    if before is None:
        return scan()
    with _LOCK:
        try:
            kept = _KEPT.get(db)
        except TypeError:  # not weakly referenceable: no reuse
            return scan()
        hit = kept.get(key) if kept else None
        if hit is not None and _same(hit[0], before):
            return _clone_rows(hit[1])
    rows = scan()
    after = _stamp(db)
    if after is not None and _same(before, after):
        with _LOCK:
            kept = _KEPT.setdefault(db, {})
            for old in [k for k, (stamp, _rows) in kept.items() if not _same(stamp, after)]:
                del kept[old]
            kept[key] = (after, _clone_rows(rows))
            while len(kept) > _MAX_KEYS:
                del kept[next(iter(kept))]
    return rows


def session_cwds(db) -> list[dict[str, Any]]:
    """``db.distinct_session_cwds()`` per store commit."""
    return read(db, ("distinct_session_cwds",), lambda: list(db.distinct_session_cwds()))


def clear() -> None:
    with _LOCK:
        _KEPT.clear()
