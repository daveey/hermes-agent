"""The sync loop's kept read-only board connections (moved out of ``kanban_sync_client``). [daveey]"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_lockguard as _kanban_lockguard
from hermes_cli import kanban_sync_cpu as _sync_cpu
from hermes_cli import kanban_sync_wal_evidence as _wal_evidence
from hermes_cli.sqlite_safe_read import pread_lock_safe


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


def schema_rows(conn: sqlite3.Connection) -> list:
    """The schema cookie and the ``sqlite_schema`` rows as *conn* reads them from the file now.

    The rows are plain table content at page 1, read without consulting what the connection
    parsed from them earlier, so a kept reader can be asked whether its parsed schema still
    describes the file. Neither half answers that alone. A restored image carries its own
    cookie, which can equal the live one over a different schema, so the rows are compared.
    SQLite re-parses a reader's schema by itself when a schema change arrives through the WAL,
    so the rows stored at open stop being the ones its parse came from; the cookie stored with
    them then differs from the file's, unless the file is back under the open-time cookie, and
    then the rows statement makes SQLite parse it again. Kept as rows and compared with ``==``:
    hashing them cost four times the read (0.63 ms against 0.16 ms for the hub board's 239
    rows, 2026-11-22)."""
    return (conn.execute("PRAGMA schema_version").fetchall()
            + conn.execute("SELECT type, name, tbl_name, rootpage, sql FROM sqlite_schema ORDER BY rowid").fetchall())


def parsed_from(conn: sqlite3.Connection, real: str) -> Optional[list]:
    """What a reader's parsed schema was built from, as the file has it now; None = cannot tell.

    ``schema_rows`` plus the main file's schema format number (header bytes 44..47), which the
    parse also holds and no statement reports: a format-1 image under the same cookie and rows
    stores a DESC index in ascending order, and a reader that parsed format 4 walks it the wrong
    way (wrong rows, no error). SQLite writes the number once, with the first table, so on a
    live board the main file's copy never changes after that. Read with a lock-safe ``pread``."""
    header = pread_lock_safe(real, 44, 4)
    return None if header is None else [header] + schema_rows(conn)


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
        # path -> (file identity, reader, generation, wal state, data_version of that wal state,
        #          resolved path, file format + schema cookie + rows, or None)
        self._conns: dict[str, tuple] = {}
        self._gen = 0
        self._answers: dict[str, dict[str, tuple[Any, Any]]] = {}  # path -> tag -> (data_version, answer)

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
        if ent is not None and self._kept_over_checkpoint(key, ent, identity):
            return ent[1], ent[2]
        self._drop(key)
        if len(self._conns) >= self.MAX:
            self._drop(next(iter(self._conns)))
        began = time.thread_time()
        conn = _kanban_lockguard.connect(f"{Path(key).as_uri()}?mode=ro", uri=True, timeout=1,
                                         check_same_thread=False)
        # Pager freshness is not a repair-input generation: checkpoints change
        # mtime/size without replacing the database. _drop already discarded
        # answers; keep the generation so ordinary checkpoints respect repair
        # windows. Replacement, reset and failed-read recovery get a new one.
        if ent is not None and ent[0][:2] == identity[:2]:
            gen, reason = ent[2], "changed"
        else:
            self._gen += 1
            gen, reason = self._gen, "first" if ent is None else "replaced"
        # The checkpoint baseline is read after the stat it is stored with: whatever moves the
        # file between the two leaves the next comparison without proof, so it reopens.
        real = os.path.realpath(key)  # SQLite names the sidecars by the resolved path
        self._conns[key] = (identity, conn, gen, _wal_evidence.read(real), None, real,
                            self._opened_schema(conn, key, real, identity))
        _sync_cpu.note_reader_open(time.thread_time() - began, reason)
        return conn, gen

    @staticmethod
    def _opened_schema(conn: sqlite3.Connection, key: str, real: str,
                       identity: tuple[int, int, int, int]) -> Optional[list]:
        """What this new reader parsed its schema from, for a later keep to compare; None = never keep.

        This first statement makes the reader parse the schema and then reads the rows. A file that
        moved since the stat may have been swapped between those two steps, so the rows are only
        trusted when the file still has the identity the reader is stored with. An unreadable schema
        is left for the caller's own first read to report."""
        try:
            rows = parsed_from(conn, real)
            st = os.stat(key)
        except (sqlite3.Error, OSError):
            return None
        return rows if (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size) == identity else None

    def _kept_over_checkpoint(self, key: str, ent: tuple, identity: tuple[int, int, int, int]) -> bool:
        """Keep the reader when SQLite's wal-index proves the main file moved in a checkpoint.

        A checkpoint leaves the database content alone and a WAL reader is built to outlive it;
        reopening only re-parsed the schema (about 3 ms per board each time, MacBook 2026-11-22).
        Kept only for the same inode, no smaller, with proof from ``kanban_sync_wal_evidence``;
        a raw copy or restore shows none and takes the reopen path above. The proof says a
        checkpoint ran, not that nothing else wrote the file, so the kept reader also gives up
        its page cache and its answers, as a new one would. Only its parsed schema survives,
        and only while the file's format number, schema cookie and ``sqlite_schema`` rows, read
        again here, are the ones the reader had when it opened: a restored image with another
        schema under the same cookie reopens (SQLite's own cookie check would let the stale parse
        answer from the wrong pages), and so does any file whose cookie moved, because the reader
        may since have parsed a later schema than the stored rows. A reader stored without them
        (None), or a file whose header cannot be read now, is never kept."""
        if ent[0][:2] != identity[:2] or identity[3] < ent[0][3]:
            return False
        began = time.thread_time()
        now = _wal_evidence.read(ent[5])
        if not _wal_evidence.checkpointed(ent[3], now):
            return False
        try:
            ent[1].execute("PRAGMA shrink_memory")
            parsed = parsed_from(ent[1], ent[5])
            if parsed is None or parsed != ent[6]:
                return False
        except sqlite3.Error:
            return False
        self._answers.pop(key, None)
        self._conns[key] = (identity, ent[1], ent[2], now, ent[4], ent[5], ent[6])
        _sync_cpu.note_reader_keep(time.thread_time() - began)
        return True

    def _rebaseline(self, key: str, version: Any) -> None:
        """Move the checkpoint baseline up to the commits this reader has now seen.

        A fully backfilled baseline cannot prove the next checkpoint once the log has restarted
        twice; one taken after each new commit still has that commit's frames outstanding. Only
        after ``_conn`` accepted the file as it is, so the baseline still follows its stat."""
        ent = self._conns.get(key)
        if ent is not None and ent[4] != version:
            self._conns[key] = (*ent[:3], _wal_evidence.read(ent[5]), version, *ent[5:])

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
        return self.read(path, sql, lambda conn: conn.execute(sql).fetchone(), unchanged_ok=unchanged_ok)

    def read(self, path: Path, tag: str, compute: Callable[[sqlite3.Connection], Any], *,
             unchanged_ok: bool = False) -> Optional[tuple[int, Any]]:
        """(generation, ``compute(conn)``) on the kept connection, or None when the file cannot be read.

        ``unchanged_ok``: the answer stored under *tag* is reused while this connection's ``data_version`` has
        not moved (no other connection committed) and the kept reader was not reopened. Each tag has its own
        answer, so two questions about one board never evict each other. ``compute`` must be a pure function
        of the database content (no clock, config or process state) and must leave no transaction open: it
        runs under the watch lock on a connection other readers share. A SQLite error drops the reader and
        every answer for the path; any other exception propagates and stores nothing."""
        with self._lock:
            key = str(Path(path).absolute())
            try:
                got = self._conn(path)
                if got is None:
                    return None
                conn, gen = got
                version = conn.execute("PRAGMA data_version").fetchone()[0] if unchanged_ok else None
                if unchanged_ok:
                    self._rebaseline(key, version)
                cached = self._answers.get(key, {}).get(tag)
                if unchanged_ok and cached is not None and cached[0] == version:
                    return gen, cached[1]
                answer = compute(conn)
                if unchanged_ok:
                    # Version is read BEFORE the query: a concurrent commit may cause
                    # one extra read, but cannot publish a stale answer as current.
                    self._answers.setdefault(key, {})[tag] = (version, answer)
                return gen, answer
            except sqlite3.Error:
                self._drop(key)
                return None

    def release_gone(self) -> int:
        """Close every kept connection whose board path no longer exists; returns how many."""
        with self._lock:
            gone = [key for key in self._conns if path_gone(key)]
            for key in gone:
                self._drop(key)
        if gone:  # the checkpoint baseline reads each board's -shm through a kept pread fd
            from hermes_cli.sqlite_safe_read import release_unlinked_fds
            release_unlinked_fds()
        return len(gone)

    def reset(self) -> None:
        with self._lock:
            for key in list(self._conns):
                self._drop(key)
