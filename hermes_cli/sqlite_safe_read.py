"""Lock-safe inspection of SQLite database files.

POSIX advisory locks are cancelled **process-wide** by ``close()`` on *any* fd for that file, so a
bare ``open(db_path, "rb") ... close()`` on a live database drops every lock SQLite holds from this
process (a VACUUM's EXCLUSIVE lock, an in-flight BEGIN IMMEDIATE's RESERVED lock). This module
tracks live connections so raw reads happen only when none exist.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import sqlite3
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Guards BOTH the registry and the lifecycle syscalls it describes. Reentrant
# because connect_tracked -> _canonical_db_path -> ... stays on one thread.
_live_lock = threading.RLock()
# canonical path -> number of live connections opened by this process
_live_connections: dict[str, int] = {}
# Keys whose connection SQLite has closed, queued from wherever that happened (possibly inside a
# garbage collection) and applied under _live_lock by the next registry read. Lock-free on purpose:
# a collection can run while any lock is held, so it must never wait for _live_lock.
_released: deque[str] = deque()
# Private, randomised name so no caller overloads it by accident (that would free the release
# object early, while the connection is still open).
_RELEASE_FUNCTION = f"_hermes_release_{secrets.token_hex(8)}"


class UntrackableConnectionError(RuntimeError):
    """A connection to a probe-able database could not be tracked. Raised rather than returning an
    untracked connection: on these paths tracking is part of the correctness contract."""


class LiveConnectionError(RuntimeError):
    """A raw file operation was attempted on a database with live connections."""


def _key(path: Path | str) -> str:
    """Canonicalise a *filesystem* path for use as a registry key."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _canonical_db_path(conn: sqlite3.Connection) -> Optional[str]:
    """The on-disk path of ``main`` as SQLite reports it (immune to ``file:`` URIs, relative paths,
    symlinks). ``None`` for in-memory/unnamed databases, which cannot be byte-probed."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if not row or len(row) < 3 or not row[2]:
        return None
    return _key(row[2])


def track_connection(path: Path | str) -> None:
    """Record that this process holds a connection to *path* (prefer :func:`connect_tracked`; this
    is for callers managing their own connection objects, and for tests)."""
    with _live_lock:
        _track_key(_key(path))


def _track_key(key: str, delta: int = 1) -> None:
    """Adjust the live count for an already-canonical key (caller holds ``_live_lock``)."""
    remaining = _live_connections.get(key, 0) + delta
    if remaining > 0:
        _live_connections[key] = remaining
    else:
        _live_connections.pop(key, None)


def untrack_connection(path: Path | str) -> None:
    """Record that one connection to *path* has been closed."""
    with _live_lock:
        _track_key(_key(path), -1)


def _apply_releases() -> None:
    """Untrack every connection SQLite has finished closing (caller holds ``_live_lock``)."""
    while _released:
        _track_key(_released.popleft(), -1)


class _ReleaseOnClose:
    """Registered as a SQL function on each tracked connection, so SQLite holds the only reference.
    SQLite frees a connection's functions after it has closed the database files (btrees first,
    then the function table, in ``sqlite3LeaveMutexAndCloseZombie``; not an API promise, which
    the tests pin), so this object is released only once the descriptors are gone: after ``close()``, or when pysqlite closes a
    connection collected without it, on any thread (#149291). It only queues its key."""

    __slots__ = ("armed", "key")

    def __init__(self, key: str) -> None:
        self.key = key
        self.armed = False

    def __call__(self) -> None:
        return None

    def __del__(self) -> None:
        if self.armed and _released is not None:  # None only during interpreter teardown
            _released.append(self.key)


def _live_main_key(key: str) -> Optional[str]:
    """The tracked main-database key that makes *key* live, or ``None`` (caller holds ``_live_lock``).

    SQLite locks the main file and its WAL sidecars; a raw ``close()`` of any of those
    inodes cancels this process's POSIX locks, but the registry is keyed by the main path."""
    _apply_releases()
    if key in _live_connections:
        return key
    for suffix in ("-wal", "-shm"):
        if key.endswith(suffix) and key[:-len(suffix)] in _live_connections:
            return key[:-len(suffix)]
    return None


def has_live_connection(path: Path | str) -> bool:
    """Whether this process holds a connection to *path* (or to the database it is a sidecar of).

    Point-in-time answer: a raw open/close right after it returns ``False`` can still race a
    new connection. Hold :func:`offline_file_access` across the I/O whenever possible."""
    with _live_lock:
        return _live_main_key(_key(path)) is not None


class _TrackingMixin:
    """Untrack-on-close behaviour, mixable into any Connection subclass.

    The real ``close()`` and the unregister happen together under ``_live_lock`` so a concurrent
    probe can never observe "no live connection" while this descriptor is still open. Unregister
    runs only after the files are closed, through :class:`_ReleaseOnClose`: a failing close (e.g.
    cross-thread ProgrammingError) keeps the entry while the FD stays open (#75629).
    """

    _hermes_tracked_path: str | None = None

    def close(self) -> None:  # type: ignore[misc]
        with _live_lock:
            super().close()  # type: ignore[misc]
            self._hermes_tracked_path = None
            _apply_releases()


class TrackedConnection(_TrackingMixin, sqlite3.Connection):
    """A ``sqlite3.Connection`` that untracks its path exactly once on close (callers close in many
    places, some via ``contextlib.closing``, so counting closes by hand is unreliable)."""


_tracked_factory_cache: dict[type, type] = {}


def _tracking_factory(factory: type) -> type:
    """Return *factory* augmented with untrack-on-close. Callers legitimately pass their own
    ``Connection`` subclasses (tests simulate FTS5-less or pragma-failing runtimes); leaving them
    untracked would quietly unguard the database, so the tracking ``close()`` is mixed in."""
    if factory is sqlite3.Connection:
        return TrackedConnection
    if issubclass(factory, _TrackingMixin):
        return factory
    cached = _tracked_factory_cache.get(factory)
    if cached is None:
        cached = type(f"Tracked{factory.__name__}", (_TrackingMixin, factory), {})
        _tracked_factory_cache[factory] = cached
    return cached


@contextlib.contextmanager
def _tracking_access(blocking):
    if not _live_lock.acquire(blocking=blocking):
        exc = sqlite3.OperationalError("database is locked: SQLite tracking registry is busy")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise exc
    try:
        yield
    finally:
        _live_lock.release()


def connect_tracked(
    path: Path | str, *, tracking_path: Path | str | None = None, connect_fn=None,
    wait_for_tracking=True, **kwargs,
) -> sqlite3.Connection:
    """``sqlite3.connect`` that registers the connection for the lifetime of the fd (released on
    ``close()``). Use for any database that might be byte-probed (``state.db``, ``kanban.db``).
    Open and registration happen together under ``_live_lock`` so a concurrent
    :func:`read_header_bytes_preopen` cannot slip between them and cancel this connection's locks."""
    opener = connect_fn if connect_fn is not None else sqlite3.connect
    kwargs["factory"] = _tracking_factory(kwargs.get("factory", sqlite3.Connection))

    with _tracking_access(wait_for_tracking):
        conn = opener(str(path), **kwargs)
        try:
            resolved = _key(tracking_path) if tracking_path is not None else _canonical_db_path(conn)
            if resolved is None:
                return conn  # in-memory / unnamed: nothing on disk to byte-probe
            if not isinstance(conn, _TrackingMixin):
                # The opener substituted its own factory and discarded ours (test doubles
                # simulating FTS5-less runtimes do this). Retag the instance's class so close()
                # still releases the registry entry rather than silently losing probe safety.
                conn = _retrofit_tracking(conn, resolved)
            _track_key(resolved)
            release = _ReleaseOnClose(resolved)
            try:
                # Confirm registration without running SQL (a statement would read the schema and
                # wait on locks): sqlite3 holds a reference to the callable while it is registered.
                before = sys.getrefcount(release)
                conn.create_function(_RELEASE_FUNCTION, 0, release)
                if sys.getrefcount(release) == before:  # dropped: nothing would release the entry
                    raise sqlite3.ProgrammingError("connection factory did not register the release function")
            except Exception:
                _track_key(resolved, -1)  # the release object can never fire: take the count back
                raise
            release.armed = True
            conn._hermes_tracked_path = resolved
            return conn
        except Exception:
            try:
                # Close via sqlite3 directly: the tracking entry was either never made or is
                # being unwound here.
                sqlite3.Connection.close(conn)
            except Exception:
                pass
            raise


def _retrofit_tracking(conn: sqlite3.Connection, resolved: str) -> sqlite3.Connection:
    """Give an already-open connection untrack-on-close semantics by swapping ``__class__`` for
    one mixing in the tracking ``close()`` (used when an opener ignored the factory we asked for)."""
    cls = type(conn)
    try:
        conn.__class__ = _tracking_factory(cls)  # type: ignore[assignment]
        return conn
    except TypeError as exc:
        raise UntrackableConnectionError(
            f"connection to {resolved} uses factory {cls.__name__}, which "
            "cannot release its tracking entry on close; byte-probe safety "
            "for this database would be silently lost"
        ) from exc


def page_count_bytes(conn: sqlite3.Connection) -> Optional[int]:
    """Logical database size in bytes (``page_count * page_size``, the header field at offset 28)
    read via PRAGMA over *conn* so no new fd is opened. ``None`` when the pragmas cannot be read."""
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count) * int(page_size)
    except (sqlite3.Error, TypeError, IndexError, ValueError) as exc:
        logger.debug("page_count/page_size unavailable: %s", exc)
        return None


def file_length_matches_header(conn: sqlite3.Connection) -> Optional[bool]:
    """Whether the file on disk is at least as long as the header claims ("torn extend" check),
    without opening the file (PRAGMA over *conn* + ``stat()``). Advisory in WAL mode: a freshly
    committed page may still live in ``-wal`` so the main file legitimately lags."""
    path_str = _canonical_db_path(conn)
    if path_str is None:
        return None
    logical = page_count_bytes(conn)
    if not logical:
        return None
    try:
        return os.path.getsize(path_str) >= logical
    except OSError:
        return None


def read_header_bytes_preopen(path: Path | str, *, length: int = 100, force: bool = False) -> Optional[bytes]:
    """Read the first *length* bytes of *path* -- only when no connection is live. The ONLY
    sanctioned byte-level read of a database file, for first-open validation (real SQLite? zeroed?
    overwritten?). Check and open/read/close run together under ``_live_lock`` so a connection
    cannot be opened between deciding "nothing is live" and closing this descriptor."""
    with _live_lock:
        if not force and _live_main_key(_key(path)) is not None:
            logger.debug(
                "refusing byte-level read of %s: a live connection exists in "
                "this process and close() would cancel its POSIX locks",
                path)
            return None
        try:
            # Untracked read-only caches may still hold SQLite locks. On POSIX use a
            # never-closed descriptor even when this registry is empty (or force=True).
            if hasattr(os, "pread"):
                return read_header_lock_safe(path, length=length)
            with open(path, "rb") as handle:
                return handle.read(length)
        except OSError:
            return None


@contextlib.contextmanager
def offline_file_access(path: Path | str, *, what: str = "read"):
    """Hold the connection-lifecycle lock across a raw read of a database file: checking
    :func:`has_live_connection` and *then* doing raw I/O is a check/use race (a connection opened
    in between loses its POSIX locks to the raw ``close()``). Held only for the raw I/O."""
    with _live_lock:
        main = _live_main_key(_key(path))
        if main is not None:
            subject = "it" if main == _key(path) else f"its main database {main}"
            raise LiveConnectionError(
                f"Refusing to {what} {path}: a connection to {subject} is still open "
                "in this process, and raw file access would cancel that "
                "connection's POSIX advisory locks. Close all database "
                "handles (stop the gateway/dashboard) and retry.")
        yield


# ---------------------------------------------------------------------------------------------
# Lock-safe raw reads. ``open()``/``close()`` of a live database file (or its -wal/-shm) from
# inside a process that holds connections cancels that process's POSIX locks even when the
# registry above says "nothing live" (untracked read-only caches, sync readers, ...). A sibling's
# close then takes EXCLUSIVE, checkpoints and UNLINKS -wal/-shm, stranding every connection here
# on a deleted generation: integrity_check then reports phantom index errors / SQLITE_CORRUPT /
# SQLITE_IOERR on a healthy file. These helpers pread from a per-inode fd that is cached for the
# life of the process and never closed (opening cannot cancel locks; closing can), so they are
# safe whatever connections exist.
# ---------------------------------------------------------------------------------------------
_PREAD_LOCK = threading.Lock()
_PREAD_FDS: dict[str, tuple[int, int, int]] = {}  # path -> (fd, st_dev, st_ino)
_RETIRED_PREAD_FDS: list[int] = []  # superseded inodes: closed only once unlinked (see sweep)


def _fd_is_unlinked(fd: int) -> bool:
    try:
        return os.fstat(fd).st_nlink == 0
    except OSError:
        return True  # already invalid: nothing to protect, drop it from the cache


def _release_unlinked_locked() -> None:
    """Close cached reader fds whose inode no path names any more (``st_nlink == 0``).

    Closing such an fd is lock-safe: POSIX locks on a dead inode protect nothing, since no
    process can newly open it, and a connection still attached to it is already stranded.
    Keeping it open would pin a deleted -wal/-shm generation on disk forever and show up as a
    false "stranded on a deleted generation" in every ``/proc/<pid>/fd`` scan."""
    for key, entry in list(_PREAD_FDS.items()):
        if _fd_is_unlinked(entry[0]):
            _PREAD_FDS.pop(key, None)
            try:
                os.close(entry[0])
            except OSError:
                pass
    keep: list[int] = []
    for fd in _RETIRED_PREAD_FDS:
        if _fd_is_unlinked(fd):
            try:
                os.close(fd)
            except OSError:
                pass
        else:
            keep.append(fd)
    _RETIRED_PREAD_FDS[:] = keep


def release_unlinked_fds() -> None:
    """Public sweep: close lock-safe reader fds whose file has been unlinked."""
    with _PREAD_LOCK:
        _release_unlinked_locked()


def lock_safe_fds() -> frozenset[int]:
    """Descriptor numbers owned by the lock-safe readers (not connection fds). Sweeps reader
    fds on unlinked inodes first, so callers scanning ``/proc/self/fd`` see only live ones."""
    with _PREAD_LOCK:
        _release_unlinked_locked()
        return frozenset([entry[0] for entry in _PREAD_FDS.values()] + _RETIRED_PREAD_FDS)


def _pread_fd(path: Path | str) -> Optional[tuple[int, os.stat_result]]:
    key = str(path)
    try:
        st = os.stat(key)
    except OSError:
        return None
    with _PREAD_LOCK:
        _release_unlinked_locked()
        cached = _PREAD_FDS.get(key)
        if cached is not None and (cached[1], cached[2]) != (st.st_dev, st.st_ino):
            _RETIRED_PREAD_FDS.append(_PREAD_FDS.pop(key)[0])  # closed by the sweep once unlinked
            cached = None
        if cached is None:
            try:
                fd = os.open(key, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                fst = os.fstat(fd)
            except OSError:
                return None
            cached = _PREAD_FDS[key] = (fd, fst.st_dev, fst.st_ino)
        return cached[0], st


def pread_lock_safe(path: Path | str, offset: int, length: int) -> Optional[bytes]:
    """``length`` bytes at ``offset`` of *path* without ever closing a descriptor on it."""
    entry = _pread_fd(path)
    if entry is None:
        return None
    try:
        return os.pread(entry[0], length, offset)
    except OSError:
        return None


def read_header_lock_safe(path: Path | str, length: int = 100) -> Optional[bytes]:
    """First bytes of a database file; safe while connections (tracked or not) are open."""
    return pread_lock_safe(path, 0, length)


def sha256_lock_safe(path: Path | str, chunk: int = 1024 * 1024) -> Optional[str]:
    """sha256 of *path* via lock-safe preads, or ``None`` when unreadable."""
    entry = _pread_fd(path)
    if entry is None:
        return None
    digest = hashlib.sha256()
    offset = 0
    try:
        while True:
            data = os.pread(entry[0], chunk, offset)
            if not data:
                break
            digest.update(data)
            offset += len(data)
    except OSError:
        return None
    return digest.hexdigest()


def copy_file_out_of_process(src: Path | str, dst: Path | str, timeout: float = 120.0) -> bool:
    """Copy *src* to *dst* (``copy2`` semantics) in a short-lived child process.

    Use for a database's ``-wal``/``-shm``: the child's open()/close() cannot touch this
    process's POSIX locks, and no descriptor on the sidecar is left behind here. A cached
    in-process reader fd would outlive the generation once SQLite unlinks it, pinning a
    deleted -wal/-shm that external stranded-process watchdogs (which scan /proc/*/fd and may
    restart the unit) read as a stranded process."""
    import subprocess
    import sys

    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", "import shutil, sys; shutil.copy2(sys.argv[1], sys.argv[2])",
             os.fspath(src), os.fspath(dst)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def copy_file_lock_safe(src: Path | str, dst: Path | str, chunk: int = 1024 * 1024) -> bool:
    """Copy *src* to *dst* (``copy2`` semantics: bytes, mode, mtime) with lock-safe preads."""
    entry = _pread_fd(src)
    if entry is None:
        return False
    fd, st = entry
    try:
        with open(dst, "wb") as out:
            offset = 0
            while True:
                data = os.pread(fd, chunk, offset)
                if not data:
                    break
                out.write(data)
                offset += len(data)
        os.chmod(dst, st.st_mode & 0o7777)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        return False
    return True
