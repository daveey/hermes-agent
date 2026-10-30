"""Which sessions have a turn running right now, in any Hermes process. [daveey]

The sidebar learns that a turn runs from its own stream events and from the ACTIVE gateway's
``session.active_list`` poll. A turn started anywhere else never reaches it: a Kanban close-out turn
the backend wakes in a chat on a gateway the window is not looking at, a turn held by an SSH-isolated
``serve`` backend or the dashboard process, or a CLI/gateway turn. The row then painted its stored
outcome (red for a decision) or green while the agent was still working in it.

Session-list endpoints now stamp ``turn_live: true`` on each row whose compression lineage holds a
live durable turn lease (state.db ``session_turn_leases``, held from admission to flush of every turn
by any surface, keyed by the lineage root). A lease whose holder pid is provably dead does not count.
Cached for a couple of seconds because the sidebar polls.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger(__name__)

_CACHE_TTL_S = 2.0
_cache_lock = threading.Lock()
_cache: tuple[float, frozenset[str]] | None = None


def _state_db_paths() -> list[Path]:
    paths: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        paths.append(home / "state.db")
        profiles = home / "profiles"
        if profiles.is_dir():
            paths.extend(sorted(profiles.glob("*/state.db")))
    except OSError:
        _log.debug("state.db paths unavailable", exc_info=True)
    return [p for p in paths if p.is_file()]


def _open_ro(path: Path) -> sqlite3.Connection:
    """Read-only open; ``mode=ro`` refuses a WAL db whose -shm is absent, so fall back to query_only."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only=ON")
        return conn


_PID_RE = re.compile(r"(?:^|:)pid=(\d+)(?::|$)")


def _holder_dead(holder: str) -> bool:
    """Display-grade liveness: True when the holder's ``pid=`` is not a running process on this host.

    Lease reclaim refuses to probe unstamped holders (it waits out the TTL so a false reclaim never
    ends a live turn), but that would leave a crashed turn painting "working" for up to the lease TTL.
    A wrong answer here only repaints a row, so probe directly. Unparseable holders count as live."""
    match = _PID_RE.search(holder or "")
    pid = int(match.group(1)) if match else 0
    if pid <= 0 or pid == os.getpid() or os.name == "nt":
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok — nt returns above
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):  # PermissionError: alive, another user's
        return False
    return False


def _collect(now: float) -> frozenset[str]:
    live: set[str] = set()
    for path in _state_db_paths():
        try:
            conn = _open_ro(path)
        except (sqlite3.Error, OSError):
            _log.debug("turn leases unreadable in %s", path, exc_info=True)
            continue
        try:
            rows = conn.execute(
                "SELECT conversation_id, holder FROM session_turn_leases WHERE expires_at > ?", (now,)
            ).fetchall()
        except sqlite3.Error:  # no lease table yet (nothing ever ran a turn there) or a busy/odd store
            rows = []
        finally:
            conn.close()
        live.update(cid for cid, holder in rows if cid and not _holder_dead(str(holder or "")))
    return frozenset(live)


def live_turn_session_ids() -> frozenset[str]:
    """Lineage-root ids with a live turn lease in any profile's state.db, cached briefly."""
    global _cache
    mono = time.monotonic()
    with _cache_lock:
        if _cache is not None and mono < _cache[0]:
            return _cache[1]
    live = _collect(time.time())
    with _cache_lock:
        _cache = (mono + _CACHE_TTL_S, live)
    return live


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid:
            yield sid


def annotate_turn_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``turn_live: true`` on each row whose lineage holds a live turn lease; pop it otherwise.
    Never raises: the list must render without it."""
    if not rows:
        return rows
    try:
        live = live_turn_session_ids()
    except (sqlite3.Error, OSError):
        _log.debug("turn activity unavailable", exc_info=True)
        return rows
    for row in rows:
        if any(sid in live for sid in _row_ids(row)):
            row["turn_live"] = True
        else:
            row.pop("turn_live", None)
    return rows


def _invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


_reset_cache_for_tests = _invalidate_cache
