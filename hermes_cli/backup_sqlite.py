"""WAL-safe SQLite snapshots. Direct execution needs only the standard library.

Desktop invokes this file before stopping its backend, even when application
imports cannot load. Full and quick backups use the same SQLite copy operation.

Run as a script, the pre-update snapshot first tries a copy-on-write clone of
state.db (and its WAL) under SQLite's write lock, and falls back to the page
copy. Progress goes to stderr as one JSON object per line (``backup_progress``,
``backup_check``); stdout carries only the final result object.
"""
import errno
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Set by the SIGTERM handler. A Python signal handler runs between arbitrary bytecodes, including
# inside a weakref callback or __del__ (import machinery, GC), where the SystemExit it raises is
# reported as "Exception ignored" and swallowed -- the pre-flight would then run on past the
# deadline. Every long loop therefore also polls this flag and raises from a normal frame.
_terminate_signal: Optional[int] = None


def _on_sigterm(signum: int, _frame) -> None:
    global _terminate_signal
    _terminate_signal = signum
    sys.exit(128 + signum)


def _raise_if_terminated() -> None:
    if _terminate_signal is not None:
        raise SystemExit(128 + _terminate_signal)


# At most two progress lines a second; the final 100% line is always written.
_PROGRESS_INTERVAL_SECONDS = 0.5


class _ProgressReporter:
    """Machine-readable snapshot progress, one JSON object per line, for the Desktop's update overlay."""

    def __init__(self, stream=None, *, interval: float = _PROGRESS_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._stream, self._interval, self._clock = stream, interval, clock
        self._last: Optional[float] = None

    def _write(self, event: dict) -> None:
        stream = self._stream if self._stream is not None else sys.stderr
        stream.write(json.dumps(event) + "\n")
        stream.flush()

    def progress(self, done_bytes: int, total_bytes: int, *, method: str, force: bool = False) -> None:
        now = self._clock()
        if not force and self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        total = max(0, int(total_bytes))
        self._write({"event": "backup_progress", "done_bytes": min(max(0, int(done_bytes)), total),
                     "total_bytes": total, "method": method})

    def check(self) -> None:
        self._write({"event": "backup_check"})

    def lock_wait(self) -> None:
        self._write({"event": "backup_lock_wait"})


class _SQLiteBackupTimeout(RuntimeError):
    """Raised when a SQLite snapshot remains busy past its deadline."""


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        with suppress(Exception):
            conn.close()


class _SQLiteBackupRestarted(RuntimeError):
    """Raised when a foreign write restarted a stepped snapshot from page one."""


# A rollback-journal reader blocks every writer's commit while it holds the read
# lock, so that copy releases it after each step; a waiting writer's PENDING lock
# refuses the next step, so it waits for one step at most.  Activity writes give
# up after 0.5s (SessionDB._ACTIVITY_WRITE_PATIENCE_S); every hold stays under it.
_ROLLBACK_STEP_SECONDS = 0.1
_ROLLBACK_FIRST_STEP_PAGES = 256
_ROLLBACK_BUSY_SLEEP_SECONDS = 0.01
# Each foreign write restarts a stepped copy, so a continuous writer gets a bounded
# number of attempts, then one whole-copy hold if it fits the budget, else a refusal.
_ROLLBACK_MAX_RESTARTS = 20
_ROLLBACK_LOCK_HOLD_SECONDS = 0.25


def _uses_wal(src: Path) -> bool:
    # Header byte 18 (file format write version) is 2 for WAL. Read through the lock-safe cached descriptor:
    # _safe_copy_db already holds a connection to src here (and runs inside the gateway/CLI next to live
    # ones), and a plain open()/close() would cancel this process's SQLite locks on the file.
    try:
        from hermes_cli.sqlite_safe_read import read_header_lock_safe
    except ImportError:  # the standalone pre-stop script: stdlib only, and no other connection in that process
        with open(src, "rb") as handle:  # sqlite-sidecar-open: ok — stdlib-only pre-stop process
            header = handle.read(19)
    else:
        header = read_header_lock_safe(src, 19) or b""
    return len(header) == 19 and header[18] == 2


class _CopyRate:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.pages = 0
        self.total = 0

    def pages_per_second(self) -> float:
        return self.pages / max(self.seconds, 1e-6)


def _stepped_attempt(conn: sqlite3.Connection, backup_conn: sqlite3.Connection, pages: int,
                     check_busy, rate: _CopyRate) -> bool:
    """One stepped backup; False when a foreign write restarted it."""
    last_remaining: Optional[int] = None
    last_tick, last_busy = time.monotonic(), False

    def _progress(status: int, remaining: int, total: int) -> None:
        nonlocal last_remaining, last_tick, last_busy
        check_busy(status, remaining, total)
        now = time.monotonic()
        # Copy time only: drop the sleep sqlite3 takes after a busy step.
        step_seconds = max(0.0, now - last_tick - (_ROLLBACK_BUSY_SLEEP_SECONDS if last_busy else 0.0))
        last_tick, last_busy = now, status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        if status not in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
            return
        restarted = last_remaining is not None and remaining >= last_remaining
        rate.seconds += step_seconds
        rate.pages += total - remaining if last_remaining is None or restarted else last_remaining - remaining
        rate.total = total
        if restarted:
            raise _SQLiteBackupRestarted()
        last_remaining = remaining

    try:
        conn.backup(backup_conn, pages=pages, progress=_progress, sleep=_ROLLBACK_BUSY_SLEEP_SECONDS)
    except _SQLiteBackupRestarted:
        return False
    return True


def _stepped_rollback_journal_copy(conn: sqlite3.Connection, backup_conn: sqlite3.Connection,
                                   check_busy) -> None:
    rate = _CopyRate()
    pages = _ROLLBACK_FIRST_STEP_PAGES
    for _attempt in range(_ROLLBACK_MAX_RESTARTS + 1):
        if _stepped_attempt(conn, backup_conn, pages, check_busy, rate):
            return
        pages = max(_ROLLBACK_FIRST_STEP_PAGES, int(rate.pages_per_second() * _ROLLBACK_STEP_SECONDS))
    # Floored: a run where no step completed has no measured rate, and the estimate must not divide by 0.
    expected_hold = rate.total / max(rate.pages_per_second(), 1.0)
    if expected_hold > _ROLLBACK_LOCK_HOLD_SECONDS:
        raise RuntimeError(
            "state.db uses a rollback journal and is written continuously; a consistent snapshot "
            f"would block those writes for about {expected_hold:.1f}s, so it was not taken. "
            "Retry when no agent turn is running.")
    conn.backup(backup_conn, pages=-1, progress=check_busy, sleep=_ROLLBACK_BUSY_SLEEP_SECONDS)


class _OffThreadCopy:
    """One long sqlite3 call run off the main thread. Python runs signal handlers only on the main
    thread and only between bytecodes, so a one-step backup (a single C ``sqlite3_backup_step``,
    GIL released) would hold a SIGTERM until the whole copy finished; the main thread waits here in
    short joins instead, so the Desktop's pre-flight deadline interrupts the snapshot promptly."""

    def __init__(self, copy) -> None:
        self._copy, self._error = copy, None
        # health: allow HX012 -- stdlib-only file (the Desktop runs it when app imports cannot load); the copy reads no profile state
        self.thread = threading.Thread(target=self._run, name="sqlite-snapshot", daemon=True)

    def _run(self) -> None:
        try:
            self._copy()
        except BaseException as exc:  # health: allow BLE001 -- handed to the waiting main thread, which re-raises it
            self._error = exc

    def run(self, on_wait: Optional[Callable[[], None]] = None) -> None:
        self.thread.start()
        while self.thread.is_alive():
            _raise_if_terminated()
            if on_wait is not None:
                on_wait()
            self.thread.join(0.05)
        if self._error is not None:
            raise self._error

    def running(self) -> bool:
        return self.thread.is_alive()


def _run_interruptibly(conn: sqlite3.Connection, work):
    """Run one long SQLite call off the main thread so SIGTERM stops it (see _OffThreadCopy).

    A whole-database check is a single blocking statement; the main thread waits in short joins,
    and on termination interrupts the statement and exits instead of waiting it out."""
    result: list = []
    copier = _OffThreadCopy(lambda: result.append(work()))
    try:
        copier.run()
    except SystemExit:
        with suppress(Exception):
            conn.interrupt()
        raise
    return result[0]


def _safe_copy_db(src: Path, dst: Path, *, timeout_seconds: float = 10.0,
                  progress: Optional[Callable[[int, int], None]] = None) -> bool:
    """Copy a SQLite database with the backup() API (WAL-safe consistent snapshot).

    Fails closed when no consistent snapshot can be made: copying only the main file loses WAL data.
    ``progress(done_bytes, total_bytes)`` is called as the copy advances (unthrottled).
    """
    conn = backup_conn = None
    copier: Optional[_OffThreadCopy] = None
    try:
        # sqlite3.connect() creates a missing destination with the process
        # umask, which is commonly 0022 (0644).  Snapshot databases contain
        # session and tool state, so create the inode owner-only before SQLite
        # writes its first byte.  O_NOFOLLOW also refuses a planted symlink on
        # platforms that support it.  Tighten an existing internal staging
        # file as well (NamedTemporaryFile callers already create it 0600).
        if os.name != "nt":
            open_flags = os.O_WRONLY | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            secure_fd = os.open(dst, open_flags, 0o600)
            try:
                os.fchmod(secure_fd, 0o600)
            finally:
                os.close(secure_fd)
        # timeout=0.0 disables sqlite3's implicit busy wait so the progress callback owns the
        # full locked-source deadline instead of adding the default timeout before each callback.
        # Imported backups run inside the gateway/CLI alongside live board readers.
        # The standalone pre-stop script is already isolated from the backend's locks.
        try:
            from hermes_cli.kanban_lockguard import connect as source_connect
        except ImportError:
            # The pre-stop emergency script must also work with only the stdlib available.
            source_connect = sqlite3.connect
        conn = source_connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0,
                              check_same_thread=False)
        backup_conn = sqlite3.connect(str(dst), check_same_thread=False)
        busy_deadline = time.monotonic() + max(0.0, timeout_seconds)
        _raise_if_terminated()
        page_size = 0
        if progress is not None:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])

        def _check_backup_progress(status: int, remaining: int, total: int) -> None:
            nonlocal busy_deadline
            _raise_if_terminated()
            now = time.monotonic()
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if now >= busy_deadline:
                    raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds")
            else:
                busy_deadline = now + max(0.0, timeout_seconds)
                if progress is not None and total > 0:
                    progress((total - remaining) * page_size, total * page_size)

        if _uses_wal(src):
            # One step: SQLite restarts a multi-step backup whenever another connection writes the
            # source, and a WAL reader never blocks a writer however long the copy takes.
            # Run off the main thread so a SIGTERM is serviced during the copy (_OffThreadCopy).
            on_wait = None
            if progress is not None:
                # A one-step copy calls back only when it is done; the destination file grows as
                # pages land, so its size against the source's logical size is the progress.
                total_bytes = int(conn.execute("PRAGMA page_count").fetchone()[0]) * page_size

                def on_wait() -> None:
                    with suppress(OSError):
                        progress(min(dst.stat().st_size, total_bytes), total_bytes)
            copier = _OffThreadCopy(lambda: conn.backup(
                backup_conn, pages=-1, progress=_check_backup_progress, sleep=0.1))
            copier.run(on_wait)
        else:
            _stepped_rollback_journal_copy(conn, backup_conn, _check_backup_progress)
        return True
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        # Windows won't remove the partial destination while SQLite still has it open.
        _close_quietly(backup_conn)
        backup_conn = None
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    finally:
        # Interrupted mid-copy (SIGTERM -> SystemExit): the worker is still inside the C copy with
        # both connections, and close() would wait on their mutex until the copy ends. The process
        # is exiting; leave them to it, and the caller unlinks the staging file.
        if copier is None or not copier.running():
            _close_quietly(backup_conn)
            _close_quietly(conn)


# Longer than the Desktop's 30-minute pre-flight cap: a staging file this old
# has no live writer. A hard-killed pre-flight (Windows TerminateProcess)
# cannot unlink its own staging copy, and each one is a full-size database.
_ABANDONED_STAGING_SECONDS = 2 * 3600


def _reclaim_abandoned_staging(home: Path, prefix: str) -> None:
    horizon = time.time() - _ABANDONED_STAGING_SECONDS
    for staged in home.glob(f"{prefix}*.partial*"):  # + the copy's -journal
        with suppress(OSError):
            if staged.stat().st_mtime < horizon:
                staged.unlink()


# How long the clone path waits for SQLite's write lock. Short on purpose: a writer that keeps it
# longer sends the snapshot to the page copy, which needs no write lock on a WAL database.
_CLONE_LOCK_WAIT_SECONDS = 2.0
# "0" turns the clone path off (the page copy then runs as before).
_CLONE_ENV = "HERMES_PREFLIGHT_CLONE"
_FICLONE = 0x40049409  # Linux _IOW(0x94, 9, int): reflink on btrfs/xfs; ext4 refuses with EOPNOTSUPP
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _clone_file(src: Path, dst: Path, src_fd: Optional[int] = None) -> None:
    """Copy-on-write clone of ``src`` to the new file ``dst``; OSError when the filesystem cannot.

    macOS: clonefile(2) (APFS). Linux: ioctl(FICLONE) from ``src_fd``. Elsewhere: ENOTSUP."""
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        clonefile = libc.clonefile
        clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
        clonefile.restype = ctypes.c_int
        if clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), str(src))
        return
    if sys.platform.startswith("linux"):
        import fcntl
        owned = src_fd is None
        if owned:
            src_fd = os.open(src, os.O_RDONLY)  # sqlite-sidecar-open: ok — only the -wal, which SQLite never locks
        try:
            dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                fcntl.ioctl(dst_fd, _FICLONE, src_fd)
            except BaseException:
                os.close(dst_fd)
                dst.unlink(missing_ok=True)
                raise
            os.close(dst_fd)
        finally:
            if owned:
                os.close(src_fd)
        return
    raise OSError(errno.ENOTSUP, "no copy-on-write clone on this platform", str(src))


def _clone_unavailable(home: Path, probe: Path) -> Optional[str]:
    """Why the clone path cannot run here (None when it can), decided before any lock is taken.

    Python < 3.12 cannot stop the lock connection's close from checkpointing the whole WAL into
    state.db (on the main thread, where SIGTERM cannot interrupt it). A filesystem without
    copy-on-write clones (ext4, NTFS, a cross-device home) answers a throwaway probe file, so it
    never takes state.db's write lock just to fail."""
    if os.environ.get(_CLONE_ENV, "1") == "0":
        return f"turned off by {_CLONE_ENV}=0"
    if not hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE") or not hasattr(sqlite3.Connection, "setconfig"):
        return "needs Python 3.12+ (SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) so closing the lock never writes state.db"
    clone = Path(f"{probe}-clone")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, b"\0" * 4096)
        finally:
            os.close(fd)
        _clone_file(probe, clone)
    except OSError as exc:
        return f"no copy-on-write clone in {home}: {exc}"
    finally:
        probe.unlink(missing_ok=True)
        clone.unlink(missing_ok=True)
    return None


def _clone_snapshot(source: Path, staged: Path, reporter: Optional[_ProgressReporter] = None) -> dict:
    """Clone state.db (and its WAL) while holding SQLite's write lock, then release it.

    Under BEGIN IMMEDIATE no other connection can commit, restart or truncate the WAL, or delete it
    on close; a concurrent passive checkpoint only copies frames that stay in the WAL, which the
    clone replays when it is opened. So the two clones together are one committed state, however
    long apart they are taken. Only the clone itself runs under the lock."""
    _raise_if_terminated()
    wal_mode = _uses_wal(source)
    source_fd: Optional[int] = None
    lock: Optional[sqlite3.Connection] = None
    try:
        if sys.platform.startswith("linux"):
            # POSIX locks die with ANY close() of the file in this process, so the database's clone
            # source is opened before the lock connection and closed only after it is gone.
            source_fd = os.open(source, os.O_RDONLY)  # sqlite-sidecar-open: ok — opened before and closed after the lock connection
        try:
            lock = sqlite3.connect(str(source), timeout=0, isolation_level=None, check_same_thread=False)
            # As the last connection to close, SQLite would checkpoint the whole WAL into state.db:
            # a write the snapshot must not make (_clone_unavailable requires Python 3.12+ for this).
            lock.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            waited_at = time.monotonic()
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                # Another writer holds it: say so, then wait a bounded while (a longer holder sends
                # the snapshot to the page copy, which needs no write lock on a WAL database).
                if reporter is not None:
                    reporter.lock_wait()
                lock.execute(f"PRAGMA busy_timeout = {int(_CLONE_LOCK_WAIT_SECONDS * 1000)}")
                lock.execute("BEGIN IMMEDIATE")
            locked_at = time.monotonic()
            _clone_file(source, staged, source_fd)
            if wal_mode and Path(f"{source}-wal").exists():
                # sqlite-sidecar-open: ok — SQLite never locks the -wal itself (WAL locks live in -shm and state.db)
                _clone_file(Path(f"{source}-wal"), Path(f"{staged}-wal"))
            lock_ms = (time.monotonic() - locked_at) * 1000
            lock_wait_ms = (locked_at - waited_at) * 1000
        finally:
            if lock is not None:
                with suppress(sqlite3.Error):
                    lock.execute("ROLLBACK")
                lock.close()
    finally:
        # Its own finally: it must close even when closing the lock connection raised.
        if source_fd is not None:
            os.close(source_fd)
    # A clone keeps the source's mode (never wider than state.db itself); snapshots are owner-only.
    for clone in (staged, Path(f"{staged}-wal")):
        if clone.exists():
            os.chmod(clone, 0o600)
    return {"lock_ms": round(lock_ms, 1), "lock_wait_ms": round(lock_wait_ms, 1)}


@contextmanager
def _closing_unless_terminated(connection: sqlite3.Connection):
    """Close the connection, except when SIGTERM interrupted it: its worker thread may still be
    inside the statement, close() would wait for it, and the process is exiting anyway."""
    try:
        yield connection
    except SystemExit:
        raise
    except BaseException:
        connection.close()
        raise
    connection.close()


def _verify_clone(staged: Path) -> None:
    """Light check of a clone: the header is SQLite's, it opens, the schema reads, and its WAL folds
    back into the one file a snapshot is.

    Deliberately no PRAGMA quick_check (on 21.7 GB it ran 5+ minutes): the clone is the source's
    committed bytes taken under the write lock, so the check would only re-check state.db itself.
    The trade-off: on this path a state.db that is already corrupt is backed up as it is instead of
    cancelling the update, as the page copy's quick_check would."""
    with open(staged, "rb") as handle:  # sqlite-sidecar-open: ok — our own clone, before any connection to it
        if handle.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
            raise RuntimeError("cloned snapshot has no SQLite header")
    connection = sqlite3.connect(str(staged), check_same_thread=False)  # health: allow HX015 -- staged clone written by this function
    with _closing_unless_terminated(connection):
        connection.execute("PRAGMA schema_version").fetchone()
        if Path(f"{staged}-wal").exists():
            busy, _log, _done = _run_interruptibly(
                connection, lambda: connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            if busy:
                raise RuntimeError("cloned snapshot's WAL could not be checkpointed")
    wal = Path(f"{staged}-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("cloned snapshot kept a non-empty WAL")
    _discard_sidecars(staged, ("-wal", "-shm"))


def _discard_sidecars(staged: Path, suffixes=("-wal", "-shm", "-journal", "-probe", "-probe-clone")) -> None:
    for suffix in suffixes:
        Path(f"{staged}{suffix}").unlink(missing_ok=True)


def _quick_check(staged: Path, reporter: Optional[_ProgressReporter]) -> None:
    if reporter is not None:
        reporter.check()
    connection = sqlite3.connect(str(staged), check_same_thread=False)  # health: allow HX015 -- staged copy written by this function
    with _closing_unless_terminated(connection):
        # A whole-database check is one long statement: off the main thread, so SIGTERM stops it.
        result = _run_interruptibly(connection, lambda: connection.execute("PRAGMA quick_check").fetchall())
    if result != [("ok",)]:
        raise RuntimeError(f"SQLite snapshot integrity check failed: {result}")


def preflight_state_db(home: Path, *, busy_timeout_seconds: float = 10.0, clone: bool = False,
                       reporter: Optional[_ProgressReporter] = None) -> dict:
    """Publish an emergency snapshot; do not prune recovery files on failure.

    ``clone`` tries a copy-on-write clone first. Only the standalone script passes it: on Linux the
    clone opens state.db directly, which is safe only where no other connection to it lives in this
    process. ``reporter`` receives the progress events."""
    source = home / "state.db"
    if not source.exists():
        return {"path": None, "message": "state.db not found (fresh install?)"}
    prefix = "state.db.pre-update-emergency-"
    _reclaim_abandoned_staging(home, prefix)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    destination = home / f"{prefix}{stamp}-{os.getpid()}.bak"
    # Named before it exists, so a SIGTERM at any point after creation reaches the
    # cleanup below; _safe_copy_db creates it owner-only and refuses a symlink.
    staged = home / f"{prefix}{stamp}-{os.getpid()}.partial"
    result: dict = {}
    try:
        cloned = False
        unavailable = _clone_unavailable(home, Path(f"{staged}-probe")) if clone else None
        if unavailable:
            logger.warning("Copy-on-write snapshot skipped, copying instead: %s", unavailable)
            result = {"clone_error": unavailable}
        elif clone:
            try:
                result.update(_clone_snapshot(source, staged, reporter))
                # Light check only (header, open, schema_version, WAL fold): see _verify_clone.
                _verify_clone(staged)
                cloned = True
            except Exception as exc:
                # A held write lock, a failed or bad clone: copy instead.
                logger.warning("Copy-on-write snapshot unavailable, copying instead: %s", exc, exc_info=True)
                result = {"clone_error": str(exc)}
                staged.unlink(missing_ok=True)
                _discard_sidecars(staged)
        if cloned:
            size = staged.stat().st_size
            result["method"] = "clone"
            if reporter is not None:
                reporter.progress(size, size, method="clone", force=True)
        else:
            progress = None
            if reporter is not None:
                def progress(done: int, total: int) -> None:
                    reporter.progress(done, total, method="copy")
            if not _safe_copy_db(source, staged, timeout_seconds=busy_timeout_seconds, progress=progress):
                raise RuntimeError("SQLite safe copy failed; previous emergency snapshots were retained")
            size = staged.stat().st_size
            result["method"] = "copy"
            if reporter is not None:
                reporter.progress(size, size, method="copy", force=True)
            _quick_check(staged, reporter)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
        # A copy stopped mid-write leaves the staging copy's rollback journal (or a clone's WAL) behind too.
        _discard_sidecars(staged)
    for old in sorted(home.glob(f"{prefix}*.bak"), reverse=True)[2:]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Could not prune emergency snapshot %s: %s", old, exc)
    return {"path": str(destination), "bytes": size, **result}


if __name__ == "__main__":
    # The Desktop stops a pre-flight past its deadline with SIGTERM; exit through
    # the staging cleanup instead of dying with a full-size .partial left behind.
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Optional argv[2]: tests hold the source locked as a barrier and need the lock wait to outlast a starved runner.
    _busy = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    print(json.dumps(preflight_state_db(Path(sys.argv[1]), busy_timeout_seconds=_busy, clone=True,
                                        reporter=_ProgressReporter())))
