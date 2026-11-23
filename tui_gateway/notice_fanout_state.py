"""Host-durable cursors of ``notice_fanout.card_change_pass``. [daveey]

The card-change pass keeps, per board, the last ``task_events`` id it read and, for remote cards, the last
``chat -> {(owner-qualified key, status)}`` snapshot. In memory, a new process's first pass could only seed them, so
the changes of one interval were never fanned out. Under the host loop lease (design t_01a1d45d75851804 §2b, R3) the
pass moves between processes on every hand-off (gateway restart, a Desktop backend taking over), so each hand-off
would drop an interval. Both now live in ``<kanban home>/kanban/notice-fanout.db``, shared by every process of the
install; the next holder resumes where the last one stopped. Each process opens the store once (one connection per
path and pid, PRAGMA + schema at open) and reuses it; a failed statement drops it so the next call reopens.

Writes are last-writer-wins: with the lease off every process runs the pass, and two passes over the same
interval write the same (idempotent) refreshes. Memory caches only successfully committed writes; an unavailable
store never acknowledges new work in memory. Writers return False so executors retain replay and retry. Never raises
for SQLite failures.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

FILENAME = "notice-fanout.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (ident TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS remote_snapshot (chat TEXT PRIMARY KEY, cards TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""
_SEEDED = "remote_seeded"

_mu = threading.Lock()
# Fallback when the durable store is unavailable (per path, so a test home or HERMES_KANBAN_HOME switch is apart).
_memory: dict[str, dict] = {}
_warned: set[str] = set()
# One connection per (path, pid), opened (PRAGMA + schema) once: the pass reads and writes a cursor per board per
# tick. Keyed by pid so a forked child never shares its parent's connection; used only under ``_mu``.
_conns: dict[tuple[str, int], sqlite3.Connection] = {}

Snapshot = dict[str, frozenset[tuple[str, str]]]


def path() -> Path:
    from hermes_cli.kanban_db import kanban_home
    return kanban_home() / "kanban" / FILENAME


def _mem(where: Path) -> dict:
    return _memory.setdefault(str(where), {"cursors": {}, "remote": None})


@contextlib.contextmanager
def _open() -> Iterator[Optional[sqlite3.Connection]]:
    """This process's connection to the store (caller holds ``_mu``), or None when it cannot be opened."""
    where = path()
    key = (str(where), os.getpid())
    conn = _conns.get(key)
    if conn is None:
        try:
            where.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(where), timeout=5.0, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
                conn = None
            _warn_once(where, exc)
        else:
            _conns[key] = conn
    try:
        yield conn
    finally:
        # The connection outlives the call, so a non-sqlite error between BEGIN IMMEDIATE and COMMIT (a TypeError
        # from ``_encode``) would keep the write lock, and the next write would join that transaction and be lost.
        if conn is not None and _conns.get(key) is conn and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as exc:
                _failed(exc)


def _failed(exc: BaseException) -> None:
    """A statement failed: warn once and drop the cached connection, so the next call opens a fresh one."""
    where = path()
    _warn_once(where, exc)
    conn = _conns.pop((str(where), os.getpid()), None)
    if conn is not None:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _close_all() -> None:
    for key in [k for k in _conns if k[1] == os.getpid()]:
        with contextlib.suppress(sqlite3.Error):
            _conns.pop(key).close()
    _conns.clear()


def _warn_once(where: Path, exc: BaseException) -> None:
    if str(where) not in _warned:
        _warned.add(str(where))
        logger.warning("notice fan-out: %s unavailable (%s); keeping its cursors in this process's memory",
                       where, exc)


def get_cursor(ident: str, *, strict: bool = False) -> Optional[int]:
    with _mu, _open() as conn:
        if conn is None:
            if strict:
                raise RuntimeError("card-change cursor store unavailable")
            return _mem(path())["cursors"].get(ident)
        try:
            row = conn.execute("SELECT cursor FROM cursors WHERE ident = ?", (ident,)).fetchone()
        except sqlite3.Error as exc:
            _failed(exc)
            if strict:
                raise RuntimeError("card-change cursor store unavailable") from exc
            return _mem(path())["cursors"].get(ident)
        return int(row[0]) if row else None


def set_cursor(ident: str, cursor: int) -> bool:
    """Acknowledge only a durable commit; failed writes retain the replay position."""
    with _mu, _open() as conn:
        if conn is None:
            return False
        try:
            conn.execute("INSERT INTO cursors (ident, cursor) VALUES (?, ?) "
                         "ON CONFLICT(ident) DO UPDATE SET cursor = excluded.cursor", (ident, int(cursor)))
        except sqlite3.Error as exc:
            _failed(exc)
            return False
        _mem(path())["cursors"][ident] = int(cursor)
        return True


def _decode(raw: str) -> frozenset[tuple[str, str]]:
    try:
        return frozenset((str(k), str(s)) for k, s in json.loads(raw))
    except (TypeError, ValueError):
        return frozenset()


def _encode(cards: frozenset[tuple[str, str]]) -> str:
    return json.dumps(sorted(cards))


def load_remote(*, strict: bool = False) -> Optional[Snapshot]:
    """The last remote snapshot, or None when no pass has seeded one yet."""
    with _mu, _open() as conn:
        if conn is None:
            if strict:
                raise RuntimeError("card-change snapshot store unavailable")
            return _mem(path())["remote"]
        try:
            if conn.execute("SELECT 1 FROM meta WHERE k = ?", (_SEEDED,)).fetchone() is None:
                return None
            return {str(chat): _decode(cards) for chat, cards in conn.execute("SELECT chat, cards FROM remote_snapshot")}
        except sqlite3.Error as exc:
            _failed(exc)
            if strict:
                raise RuntimeError("card-change snapshot store unavailable") from exc
            return _mem(path())["remote"]


def seed_remote(snapshot: Snapshot) -> bool:
    """Replace the whole snapshot (first pass), certifying only durable success."""
    with _mu, _open() as conn:
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM remote_snapshot")
            conn.executemany("INSERT INTO remote_snapshot (chat, cards) VALUES (?, ?)",
                             [(chat, _encode(cards)) for chat, cards in snapshot.items()])
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, '1')", (_SEEDED,))
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            _failed(exc)
            return False
        _mem(path())["remote"] = dict(snapshot)
        return True


def ack_remote(acked: dict[str, Optional[frozenset[tuple[str, str]]]]) -> bool:
    """Record successful actions; failure never hides pending work behind a memory ACK."""
    if not acked:
        return True
    with _mu, _open() as conn:
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            for chat, cards in acked.items():
                if cards is None:
                    conn.execute("DELETE FROM remote_snapshot WHERE chat = ?", (chat,))
                else:
                    conn.execute("INSERT OR REPLACE INTO remote_snapshot (chat, cards) VALUES (?, ?)",
                                 (chat, _encode(cards)))
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            _failed(exc)
            return False
        mem = _mem(path())
        if mem["remote"] is not None:
            for chat, cards in acked.items():
                if cards is None:
                    mem["remote"].pop(chat, None)
                else:
                    mem["remote"][chat] = cards
        return True


def forget_memory_for_tests() -> None:
    """Drop this process's memory and connection only (a hand-off to a fresh process keeps the durable state)."""
    with _mu:
        _memory.clear()
        _close_all()


def reset_for_tests() -> None:
    """Drop both this process's memory and the durable state at the current path."""
    with _mu:
        _memory.clear()
        _warned.clear()
        _close_all()
        with contextlib.suppress(OSError):
            for suffix in ("", "-wal", "-shm"):
                Path(str(path()) + suffix).unlink(missing_ok=True)
