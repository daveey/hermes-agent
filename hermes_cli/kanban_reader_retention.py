"""Bound shared idle readers without closing a connection under its borrower."""
import atexit
from contextlib import contextmanager
import sqlite3

LIMIT = 32


@contextmanager
def reader(path):
    from hermes_cli.kanban_session_activity import cached_ro

    try:
        with cached_ro(path) as conn:
            yield conn
    finally:
        prune()



def prune(*, missing: str | None = None) -> None:
    from hermes_cli import kanban_session_activity as activity

    with activity._RO_CACHE_LOCK:
        entries = list(activity._RO_CACHE.items())
    for key, entry in entries:
        with activity._RO_CACHE_LOCK:
            if key != missing and len(activity._RO_CACHE) <= LIMIT:
                continue
        if not entry[2].acquire(blocking=False):
            continue  # temporarily over limit while readers are borrowed
        try:
            with activity._RO_CACHE_LOCK:
                if activity._RO_CACHE.get(key) is not entry:
                    continue
                del activity._RO_CACHE[key]
            entry[0].close()
        finally:
            entry[2].release()


def release_gone() -> int:
    """Close every idle shared reader whose board file no longer exists; returns how many.

    ``cached_ro`` revalidates only the path it is asked for, so a removed board's reader pinned the deleted db,
    -wal and -shm until the process exited (hermes-hub 2026-11-13). A borrowed reader is left to its borrower."""
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_sync_board_watch import path_gone

    with activity._RO_CACHE_LOCK:
        entries = list(activity._RO_CACHE.items())
    closed = 0
    for key, entry in entries:
        if not path_gone(key) or not entry[2].acquire(blocking=False):
            continue
        try:
            with activity._RO_CACHE_LOCK:
                if activity._RO_CACHE.get(key) is not entry:
                    continue
                del activity._RO_CACHE[key]
            entry[0].close()
            closed += 1
        finally:
            entry[2].release()
    from hermes_cli import kanban_waiting_reader
    closed += kanban_waiting_reader.release_gone()
    return closed


def close_all() -> None:
    """Shutdown cleanup; idle readers only, with active borrowers left alone."""
    from hermes_cli import kanban_session_activity as activity

    with activity._RO_CACHE_LOCK:
        paths = list(activity._RO_CACHE)
    for path in paths:
        try:
            prune(missing=path)
        except sqlite3.Error:
            continue


atexit.register(close_all)
