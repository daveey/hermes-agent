"""Last-snapshot reads for GET /board.

Desktop polls the board continuously. One uncached read of a busy board is about
1.3s of CPU, and every kanban commit moves the db/-wal signature, so the
signature cache misses on almost every poll. Waiting for that read on the
request timed the poll out (6-30s) and held an AnyIO worker. Serve the last
snapshot and refresh off the caller.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional
from plugins.kanban.dashboard import board_legacy

CACHE_TTL_S = 30.0
# A busy board moves the db signature on nearly every poll. Re-reading costs ~1.3s of
# CPU, so a changed signature only refreshes a snapshot at least this old.
MIN_REFRESH_S = 20.0
_cache: dict[tuple, tuple] = {}
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refresh_thread: Optional[threading.Thread] = None
# key -> Event set when the first read of that key (a request or the boot prewarm) finishes.
_first_reads: dict[tuple, threading.Event] = {}


def file_sig(path: str) -> Optional[tuple]:
    out = []
    for candidate in (path, f"{path}-wal"):
        try:
            st = Path(candidate).stat()
        except OSError:
            out.append(None)
            continue
        out.append((st.st_mtime_ns, st.st_size, st.st_ino))
    return tuple(out) if out[0] is not None else None


def _publish(key, sig, now, result) -> None:
    with _cache_lock:
        _cache[key] = (sig, now, result)


def retire(paths) -> None:
    """Drop snapshots after admission closes and all publishers have drained."""
    paths = set(map(str, paths))
    with _cache_lock:
        for key in list(_cache):
            if str(key[0]) in paths:
                del _cache[key]


def join_refresh(timeout: float = 5) -> None:
    thread = _refresh_thread
    if thread is not None:
        thread.join(timeout)


def _schedule(read: Callable[[], dict], key, sig, now) -> None:
    global _refresh_thread

    def run() -> None:
        try:
            result = read()
            _publish(key, sig, now, result)
        finally:
            board_legacy.release(reservation)
            _refresh_lock.release()

    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        reservation = board_legacy.reserve(key[0])
    except board_legacy.LegacyStopped:
        _refresh_lock.release()
        return
    thread = threading.Thread(target=run, daemon=True, name="kanban-board-refresh")
    _refresh_thread = thread
    try:
        thread.start()
    except RuntimeError:
        board_legacy.release(reservation)
        _refresh_lock.release()
        raise


@board_legacy.reader("key")
def serve(key, sig, read: Callable[[], dict], now: Optional[float] = None) -> dict:
    """Return a board payload.

    A fresh signature hit is reused. A miss that already has a snapshot returns
    that snapshot and refreshes off the caller. The first read (no snapshot)
    runs on the caller.
    """
    if now is None:
        now = time.monotonic()
    if key is None or sig is None:
        return read()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] == sig and now - hit[1] < CACHE_TTL_S:
        return dict(hit[2], now=int(time.time()))
    if hit is not None:
        if now - hit[1] < MIN_REFRESH_S:
            return dict(hit[2], now=int(time.time()))
        _schedule(read, key, sig, now)
        return dict(hit[2], now=int(time.time()))
    return _first_read(read, key, sig, now)


def _first_read(read: Callable[[], dict], key, sig, now) -> dict:
    """The first read of ``key`` runs once. A caller that arrives while another (the boot prewarm
    or an earlier poll) is reading waits for it instead of repeating the 4-5 s read under the
    GIL contention of the Desktop's cold-start burst."""
    with _cache_lock:
        pending = _first_reads.get(key)
        leader = pending is None
        if leader:
            pending = _first_reads[key] = threading.Event()
    if not leader:
        pending.wait()
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None:
            return dict(hit[2], now=int(time.time()))
        return read()  # the leader failed: read for ourselves
    try:
        result = read()
        _publish(key, sig, now, result)
        return result
    finally:
        with _cache_lock:
            _first_reads.pop(key, None)
        pending.set()


@board_legacy.reader("key")
def prewarm(read: Callable[[], dict], key, sig, now: Optional[float] = None) -> None:
    """Take the first read of ``key`` off the first Desktop poll (boot, background thread).
    A no-op if a snapshot exists or a read is already running."""
    if key is None or sig is None:
        return
    with _cache_lock:
        if key in _cache:
            return
    _first_read(read, key, sig, time.monotonic() if now is None else now)
