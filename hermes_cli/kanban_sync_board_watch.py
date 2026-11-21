"""The sync loop's kept read-only board connections (moved out of ``kanban_sync_client``). [daveey]"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_lockguard as _kanban_lockguard


def path_gone(path: str) -> bool:
    """True when nothing is at *path* any more (it, or a directory above it, was removed or renamed away).

    Only a missing path counts: a transient error such as EACCES says nothing about the file, so its kept
    handle stays until a read of that path revalidates it."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


class BoardWatch:
    """One long-lived read-only connection per board file, for the sync loop's cheap questions.

    ``outbox_pending`` runs every 0.5 s on every synced board. Opening a connection per call re-read the schema
    each time (about 13 ms of CPU per pass over 10 boards, 2.5% of a core in the gateway, 2026-10-31). A kept
    connection answers the same indexed query in microseconds. Repair scans use a transactional
    input-table revision rather than data_version: dispatcher-clock commits must not invalidate them.
    A file replaced on disk (new inode) is reopened with a new generation, so a restored revision
    never equals a cached key for the old file. A board whose file is removed is never queried again, so
    ``release_gone`` closes its connection from the loop; otherwise it pinned the deleted db, -wal and -shm
    until the gateway restarted (hermes-hub 2026-11-13). [daveey]"""

    MAX = 64

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, tuple[tuple[int, int, int, int], sqlite3.Connection, int]] = {}
        self._gen = 0
        self._answers: dict[str, tuple[int, str, Any]] = {}

    def _conn(self, path: Path) -> Optional[tuple[sqlite3.Connection, int]]:
        # Keep the requested spelling (including symlink/.. semantics). stat
        # follows the current target on every read; inode changes reopen it.
        # Aliases may own separate readers, bounded by MAX, instead of walking
        # every path component twice per unchanged query with realpath.
        key = str(Path(path).absolute())
        try:
            st = os.stat(key)
        except OSError:
            self._drop(key)
            return None
        # A same-inode restore can retain SQLite's header counter while changing
        # the contents. Reopen the pager, not just its answer memo, on main-file
        # movement; ordinary WAL commits still use data_version below.
        identity = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        ent = self._conns.get(key)
        if ent is not None and ent[0] == identity:
            return ent[1], ent[2]
        self._drop(key)
        if len(self._conns) >= self.MAX:
            self._drop(next(iter(self._conns)))
        conn = _kanban_lockguard.connect(f"{Path(key).as_uri()}?mode=ro", uri=True, timeout=1,
                                         check_same_thread=False)
        # Pager freshness is not a repair-input generation: checkpoints change
        # mtime/size without replacing the database. _drop already discarded
        # answers; keep the generation so ordinary checkpoints respect repair
        # windows. Replacement, reset and failed-read recovery get a new one.
        if ent is not None and ent[0][:2] == identity[:2]:
            gen = ent[2]
        else:
            self._gen += 1
            gen = self._gen
        self._conns[key] = (identity, conn, gen)
        return conn, gen

    def _drop(self, key: str) -> None:
        self._answers.pop(key, None)
        ent = self._conns.pop(key, None)
        if ent is not None:
            try:
                ent[1].close()
            except sqlite3.Error:
                pass

    def query(self, path: Path, sql: str, *, unchanged_ok: bool = False) -> Optional[tuple[int, Any]]:
        """(generation, first row) or None when the file cannot be read."""
        with self._lock:
            try:
                got = self._conn(path)
                if got is None:
                    return None
                conn, gen = got
                key = str(Path(path).absolute())
                version = conn.execute("PRAGMA data_version").fetchone()[0] if unchanged_ok else None
                cached = self._answers.get(key)
                if unchanged_ok and cached is not None and cached[:2] == (version, sql):
                    return gen, cached[2]
                answer = conn.execute(sql).fetchone()
                if unchanged_ok:
                    # Version is read BEFORE the query: a concurrent commit may cause
                    # one extra read, but cannot publish a stale answer as current.
                    self._answers[key] = (version, sql, answer)
                return gen, answer
            except sqlite3.Error:
                self._drop(str(Path(path).absolute()))
                return None

    def release_gone(self) -> int:
        """Close every kept connection whose board path no longer exists; returns how many."""
        with self._lock:
            gone = [key for key in self._conns if path_gone(key)]
            for key in gone:
                self._drop(key)
        return len(gone)

    def reset(self) -> None:
        with self._lock:
            for key in list(self._conns):
                self._drop(key)
