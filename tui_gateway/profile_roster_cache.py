"""Memos for ``profiles.list``'s per-profile fields, each keyed on the file(s) it derives from.

The Bots roster polls ``profiles.list`` every 5s PER CONNECTION, and two groups of fields on every
row are recomputed from disk each time even though nothing has changed:

- the **session fields** (``last_session`` / ``worker_session`` / ``canonical_session``) — a
  read-only ``state.db`` open plus a listing query plus the canonical-title lookup, per profile;
- the **ui_meta fields** (``ui_meta`` / ``ui_meta_revisions``) — a second parse of that profile's
  ``profile.yaml``, which the listing body already parsed once for description/display_name.

Both are pure functions of the files they read, so while those files have not moved there is
nothing to recompute: an idle fleet re-derived identical rows every five seconds for every bot.

Signatures carry each file's ``(mtime_ns, size)`` so a write landing inside one mtime tick still
invalidates; ``profile.yaml`` also carries its inode, because the atomic writers rename a temp file
into place. A profile missing the file a memo keys on is never cached — reading it costs nothing,
and one written later must be picked up.

Only DERIVED values live here. The raw readers keep their uncached contract: ``_read_profile_yaml``
is also used by the ui_meta CAS writer, which reads that document, mutates it and writes it back, and
a stale read there would overwrite a newer file.

This is a module of its own because ``methods_profiles``'s bodies are rebound onto ``server.py``'s
globals (``method_ctx.bind_module``), which COPIES module-level dicts rather than sharing them — a
cache declared there would be written to one copy and read from another.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from utils import mtime_is_racy

# Each keyed by resolved profile path. One entry per profile the roster has painted; a removed
# profile leaves one stale entry, so a memo is dropped whole once it grows past any plausible fleet.
_SESSION_CACHE: dict[str, tuple[tuple, dict]] = {}
_UI_META_CACHE: dict[str, tuple[tuple, dict]] = {}
# The canonical gateway's rows also depend on who reads them (its owner filter): keyed by
# (profile path, reader).
_READER_SESSION_CACHE: dict[tuple, tuple[tuple, dict]] = {}
_MAX_ENTRIES = 512

# Fills in progress, one table per memo with the memo's keys. ``profiles.list`` runs on the serve's RPC
# pool and Desktop asks for it several times at connect: without these, every concurrent cold caller
# recomputed every profile (6 calls: ~4 s of CPU on a busy hub against ~0.3 s in sequence). An entry
# lives only while its fill runs, so a table never outgrows the memo it fronts. ``_Flight`` is one entry.
class _Flight:
    """One running fill: the signature it was taken under, the thread running it, its stored copy."""

    __slots__ = ("done", "owner", "signature", "stored", "waiters")

    def __init__(self, signature: tuple, owner: int):
        self.signature = signature
        self.owner = owner
        self.done = threading.Event()
        self.stored: Optional[dict] = None
        self.waiters = 0


_SESSION_FLIGHTS: dict[str, _Flight] = {}
_UI_META_FLIGHTS: dict[str, _Flight] = {}
_READER_SESSION_FLIGHTS: dict[tuple, _Flight] = {}
_FLIGHT_LOCK = threading.Lock()
# A joiner stops waiting after this long and reads for itself: a fill that slow is stuck (a store
# locked past SQLite's busy wait), and every caller paid that wait on its own before single flight.
_JOIN_TIMEOUT_S = 30.0

_STORE_FILES = ("state.db", "state.db-wal")


def _file_parts(path: Path, *, with_inode: bool = False) -> Optional[tuple]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (path.name, stat.st_mtime_ns, stat.st_size) + ((stat.st_ino,) if with_inode else ())


def _cached(cache: dict, flights: dict, key: Any, signature: Optional[tuple], compute: Callable[[], dict],
            copier: Callable[[dict], dict]) -> dict[str, Any]:
    """``compute()``'s fields, reused while *signature* holds. No signature means no cache.

    Single flight: a miss whose signature EQUALS a fill already running for *key* waits for that fill and
    returns a copy of what it stored — the value a hit right after it would return. Any other miss leads
    its own fill; only the current fill for *key* may store, so a superseded or invalidated one never
    overwrites a newer memo. A joiner whose leader failed, or took longer than ``_JOIN_TIMEOUT_S``,
    computes for itself (after a leader failure every joiner does). A same-thread re-entry computes directly.
    """
    if signature is None:
        return compute()
    hit = cache.get(key)
    if hit is not None and hit[0] == signature:
        return copier(hit[1])
    me = threading.get_ident()
    with _FLIGHT_LOCK:
        # A fill may have stored and retired since our unlocked miss.
        hit = cache.get(key)
        flight = flights.get(key)
        if hit is not None and hit[0] == signature:
            lead = False  # Copy only after releasing the lock, just like the fast hit.
        elif flight is not None and flight.owner == me:
            flight = None  # re-entered from our own fill: never wait on ourselves
            lead = False
        elif flight is not None and flight.signature == signature:
            flight.waiters += 1
            lead = False
        else:
            flight = flights[key] = _Flight(signature, me)
            lead = True
    if hit is not None and hit[0] == signature:
        return copier(hit[1])
    if flight is None:
        return compute()
    if not lead:
        if flight.done.wait(_JOIN_TIMEOUT_S) and flight.stored is not None:
            return copier(flight.stored)
        return compute()
    try:
        fields = compute()
        stored = copier(fields)
        with _FLIGHT_LOCK:
            if flights.get(key) is flight:
                if len(cache) >= _MAX_ENTRIES:
                    cache.clear()
                cache[key] = (signature, stored)
                flight.stored = stored
        return fields
    finally:
        flight.done.set()
        with _FLIGHT_LOCK:
            if flights.get(key) is flight:
                del flights[key]


def store_signature(profile_path: str | Path) -> Optional[tuple]:
    """``(name, mtime_ns, size)`` per session-store file, or None when the profile has no store.

    The pair the change watcher already trusts for ``sessions.changed``. None too while a file's
    mtime is fresh enough that a same-tick, same-size WAL rewrite could still hide behind it.
    """
    base = Path(profile_path)
    parts = [p for p in (_file_parts(base / name) for name in _STORE_FILES) if p is not None]
    # Our own read-only open creates an EMPTY -wal sidecar on a store that never had one; it
    # carries no frames, so it must not read as "the store moved" on the very next poll.
    parts = [p for p in parts if not (p[0].endswith("-wal") and p[2] == 0)]
    if any(mtime_is_racy(p[1]) for p in parts):
        return None
    return tuple(parts) or None


def profile_yaml_signature(profile_dir: str | Path) -> Optional[tuple]:
    """``(name, mtime_ns, size, inode)`` of the profile's ``profile.yaml``, or None when absent."""
    return _file_parts(Path(profile_dir) / "profile.yaml", with_inode=True)


def cached_session_fields(profile_path: str | Path, compute: Callable[[], dict]) -> dict[str, Any]:
    """``compute()``'s fields, reused while the profile's session store has not moved."""
    return _cached(_SESSION_CACHE, _SESSION_FLIGHTS, str(profile_path), store_signature(profile_path),
                   compute, dict)


def cached_reader_session_fields(profile_path: str | Path, reader: tuple,
                                 compute: Callable[[], dict]) -> dict[str, Any]:
    """``cached_session_fields`` for fields that also depend on *reader*, a hashable principal key."""
    return _cached(_READER_SESSION_CACHE, _READER_SESSION_FLIGHTS, (str(profile_path), reader),
                   store_signature(profile_path), compute, dict)


def cached_ui_meta_fields(profile_dir: str | Path, compute: Callable[[], dict]) -> dict[str, Any]:
    """``compute()``'s fields, reused while the profile's ``profile.yaml`` has not changed.

    Copied deeply: ``ui_meta`` is a nested mapping the caller hands to a client.
    """
    return _cached(_UI_META_CACHE, _UI_META_FLIGHTS, str(profile_dir), profile_yaml_signature(profile_dir),
                   compute, copy.deepcopy)


def invalidate(profile_path: str | Path | None = None) -> None:
    """Drop one profile's memos, or all of them. For tests and for a caller that knows better.

    Fills already running for it are dropped too: no later caller joins one, and none of them stores."""
    with _FLIGHT_LOCK:
        for cache, flights in ((_SESSION_CACHE, _SESSION_FLIGHTS), (_UI_META_CACHE, _UI_META_FLIGHTS)):
            if profile_path is None:
                cache.clear()
                flights.clear()
            else:
                cache.pop(str(profile_path), None)
                flights.pop(str(profile_path), None)
        for table in (_READER_SESSION_CACHE, _READER_SESSION_FLIGHTS):
            for key in [key for key in table if profile_path is None or key[0] == str(profile_path)]:
                table.pop(key, None)
