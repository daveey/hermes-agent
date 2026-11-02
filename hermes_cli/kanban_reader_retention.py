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
