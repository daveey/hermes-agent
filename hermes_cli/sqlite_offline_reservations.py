"""Path reservations for raw SQLite I/O; registry operations need the caller's mutex.

Completion uses an Event rather than reacquiring that mutex: a legacy caller may
wait for a reservation while recursively owning the registry mutex. No SQLite
handle can be opened between checking a reservation and registering its handle.

Reservations, like the live-connection registry, are keyed by resolved path: a
hardlink alias or an ATTACHed database is not recognised as the reserved file.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit


_active: list[tuple[str, int, threading.Event]] = []
# The target cannot be predicted before SQLite parses it: exclude every reservation.
UNKNOWN = object()


def database_key(path, *, uri=False):
    """Canonical file SQLite will open for *path*; ``None`` when nothing on disk, ``UNKNOWN``
    when SQLite's own parsing could pick a file this prediction would miss."""
    try:
        value = os.fsdecode(path)
        if uri and value.startswith("file:"):
            parsed = urlsplit(value)
            query = parse_qs(parsed.query, keep_blank_values=True)
            # SQLite decodes bytes and stops at an escaped NUL; a VFS may map names anywhere.
            raw = unquote_to_bytes(parsed.path)
            if (os.name == "nt" or b"\x00" in raw or "vfs" in query
                    or parsed.netloc not in ("", "localhost")):
                return UNKNOWN
            value = os.fsdecode(raw)
            if query.get("mode") == ["memory"]:
                return None
        if value in ("", ":memory:"):
            return None
        return str(Path(value).resolve())
    except (OSError, RuntimeError, ValueError):
        return UNKNOWN


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
