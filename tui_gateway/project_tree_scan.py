"""The JSON-RPC ``projects.tree`` store reads, kept until the store commits. [daveey]

The all-profiles HTTP tree already reuses each store's session page per commit
(``web_routers.profiles._PROJECT_TREE_SCAN_CACHE``). The Desktop's own-profile RPC did not: every
``projects.tree`` re-read the 2,000-row page and aggregated ``distinct_session_cwds`` over the whole
``sessions`` table, though the store had not changed. On the hub the tui-rpc pool spent ~15k
voluntary context switches/s in that loop (t_01a20286f9f3f262). Both reads are pure projections of
``state.db`` (plus config, in the key), so they are kept until ``PRAGMA data_version`` moves, checked
once per request; there is no TTL and no timer. Annotation and the tree build still run every time.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, cast

from hermes_cli.profile_session_scan_cache import ProfileSessionScanCache

_LOCK = threading.Lock()
_CACHE = ProfileSessionScanCache()


def _home(db) -> Path | None:
    """The store's home when ``db`` is a ``<home>/state.db`` file the cache can observe, else None (no reuse)."""
    path = getattr(db, "db_path", None)
    if not path:
        return None
    path = Path(path)
    return path.parent if path.name == "state.db" else None


def _clone_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fresh row dicts for one response: annotation and tree building stamp them; their container values
    (``_lineage_ids``) are copied too, so nothing a caller mutates reaches the kept copy."""
    return [{k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict)
                 else set(v) if isinstance(v, set) else v) for k, v in row.items()} for row in rows]


def read(db, key: tuple, scan: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """``scan()``, or a copy of its last result while ``db``'s store has not committed since."""
    home = _home(db)
    if home is None:
        return scan()
    with _LOCK:
        return cast("list[dict[str, Any]]", _CACHE.read(home, key, scan, clone=_clone_rows))


def session_cwds(db) -> list[dict[str, Any]]:
    """``db.distinct_session_cwds()`` per store commit."""
    return read(db, ("distinct_session_cwds",), lambda: list(db.distinct_session_cwds()))


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
