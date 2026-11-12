"""Host-wide running-worker count for notices and card reads: tied to the asking connection's board, read-only. [daveey]

hermes-hub 2026-11-10: a ``runners`` notice said "0/7 worker slots busy" while ``default`` ran 7 cards. The notice
path called ``count_running_tasks_other_boards()`` without its board, which resolved the board to exclude from the
process's current board (``default``) instead of the connection it was reading, so ``default`` was skipped and
``runners`` counted twice. Here the board to exclude is the file the connection is attached to, other boards are
opened ``mode=ro`` (no migration, no write lock on a production DB), and any read failure makes the count
**unknown** (None), never a partial sum or zero.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

_log = logging.getLogger(__name__)


def conn_db_path(conn: sqlite3.Connection) -> Optional[Path]:
    """Resolved file behind ``conn``'s main database (None for an in-memory or unreadable connection)."""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
            if name == "main" and file:
                return Path(file).expanduser().resolve()
    except sqlite3.Error:
        _log.debug("host counts: database_list unavailable", exc_info=True)
    return None


def _board_paths() -> list[tuple[str, Path]]:
    """``(slug, resolved path)`` of every active board; raises when boards cannot be enumerated."""
    from hermes_cli import kanban_db as kb
    out: list[tuple[str, Path]] = []
    for meta in kb.list_boards(include_archived=False):
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        out.append((slug, kb.kanban_db_path(board=slug).expanduser().resolve()))
    return out


def board_of(conn: sqlite3.Connection) -> Optional[str]:
    """Slug of the board whose file ``conn`` is attached to; None when it matches no active board."""
    path = conn_db_path(conn)
    if path is None:
        return None
    try:
        return next((slug for slug, p in _board_paths() if p == path), None)
    except Exception:  # health: allow BLE001 -- an unreadable board list leaves the board unnamed, not wrong
        _log.debug("host counts: board list unavailable", exc_info=True)
        return None


def _own_gateway(conn: sqlite3.Connection) -> Optional[str]:
    """The file's own gateway id when the file says it is a ``primary`` synced board, else None.

    Read from ``kanban_sync_state`` in the file, not from ``kanban_db_sync.board_mode``: that answers ``off`` unless
    THIS process installed sync on the file, and a fenced worker's ``mode=ro`` connection (or another board's file
    opened here) never does. hermes-hub 2026-11-12: a worker's ``kanban show`` said "14/7 running" while the gateway
    held 6, because 8 cards leased to pool workers were counted as local slots. No ``kanban_sync_state`` table, no
    row, or a mode other than ``primary`` counts every running card. A primary row without a gateway id, or a
    state that cannot be read, raises (the caller reports the count unknown)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kanban_sync_state'").fetchone():
        return None
    row = conn.execute("SELECT mode, gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
    if row is None or row[0] != "primary":
        return None
    if not row[1]:
        raise ValueError("primary kanban_sync_state has no gateway_id")
    return str(row[1])


def _running_in(conn: sqlite3.Connection) -> int:
    """Running cards on ``conn``'s file this gateway owns (a card another gateway's lease holds is elsewhere: the
    same condition as ``kanban_db_sync.held_elsewhere_sql``, keyed on the file's state, see :func:`_own_gateway`)."""
    own = _own_gateway(conn)
    if own is None:
        return int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0])
    return int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running' "
        "AND NOT (lease_holder IS NOT NULL AND lease_holder != ? AND claim_lock IS NULL)", (own,)).fetchone()[0])


def _running_in_file(path: Path) -> int:
    uri = f"file:{quote(str(path))}?mode=ro"
    from hermes_cli import kanban_lockguard
    other = kanban_lockguard.connect(uri, uri=True, timeout=2.0)
    try:
        other.row_factory = sqlite3.Row
        return _running_in(other)
    finally:
        other.close()


def running_on_host(conn: sqlite3.Connection) -> dict[str, Any]:
    """``{"running": int | None, "board": slug | None, "observed_at": epoch}``.

    ``running`` sums ``conn``'s own file and every other active board's file once each (boards sharing a file,
    as under ``HERMES_KANBAN_DB``, count once). Any failure leaves it None (unknown)."""
    observed = int(time.time())
    board = board_of(conn)
    out: dict[str, Any] = {"running": None, "board": board, "observed_at": observed}
    path = conn_db_path(conn)
    if path is None:
        return out
    try:
        total = _running_in(conn)
        for other in dict.fromkeys(p for _slug, p in _board_paths()):
            if other == path or not other.exists():
                continue
            total += _running_in_file(other)
    except Exception:  # health: allow BLE001 -- any unreadable board makes the host count unknown, never partial
        _log.debug("host counts: running count unavailable", exc_info=True)
        return out
    out["running"] = total
    return out
