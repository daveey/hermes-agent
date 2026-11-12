"""Path reservations for raw SQLite I/O; registry operations need the caller's mutex.

Completion uses an Event rather than reacquiring that mutex: a legacy caller may
wait for a reservation while recursively owning the registry mutex. No SQLite
handle can be opened between checking a reservation and registering its handle.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, unquote, urlsplit


_active: list[tuple[str, int, threading.Event]] = []


def database_key(path, *, uri=False):
    value = os.fsdecode(path)
    if uri and value.startswith("file:"):
        parsed = urlsplit(value)
        value = unquote(parsed.path)
        if parse_qs(parsed.query).get("mode") == ["memory"]:
            return None
    if value in ("", ":memory:"):
        return None
    return str(Path(value).resolve())


def overlaps(left, right):
    return left == right or any(
        left == right + suffix or right == left + suffix
        for suffix in ("-wal", "-shm")
    )


def active():
    _active[:] = [entry for entry in _active if not entry[2].is_set()]
    return _active


def conflicts(key, *, raw=False, unknown=False):
    ident = threading.get_ident()
    return [entry for entry in active()
            if (unknown or (key is not None and overlaps(key, entry[0])))
            and (not raw or entry[1] != ident)]


def owns_reservation():
    return any(entry[1] == threading.get_ident() for entry in active())


def reserve(key, finished):
    _active.append((key, threading.get_ident(), finished))
