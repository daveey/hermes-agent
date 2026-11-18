"""Serve the kanban annotation inline while nothing it reads has moved. [daveey]

On David-MBP a Desktop session-list poll started a refresh thread every ~3 s (20/min) although board content moved in
3 of 60 polls (t_01a19d0bcab45558): the counts TTL (3 s) and the attention TTL (30 s) expired with unchanged inputs.
Past its TTL a cached read is still the answer while the boards' fingerprint is unchanged and proves "no write since"
(``revalidatable``), up to a max age that bounds the time-derived states no key sees. A board write moves its content
revision, so it is never hidden (read-your-writes).

State stays in ``kanban_session_activity`` (late-bound ``_ksa``: that module imports this one).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from hermes_cli.kanban_generation import is_busy

_log = logging.getLogger("hermes_cli.kanban_session_activity")

STAT_SETTLED_S = 2.0

# Set by ``refresh_if_unchanged`` while it re-reads a scope whose inputs it just found unchanged:
# ``(without shadows or None, with shadows)``, so the readers do not read every board's revision again (per thread).
inputs_pin = threading.local()
# [daveey] ``force_read.scope``: this thread's next counts read of that scope really reads (the annotation holder
# renewing an aging host copy, ``kanban_annotation_host._holder_refresh``).
force_read = threading.local()


def revalidatable(fingerprint: tuple, wall: float) -> bool:
    """True when ``fingerprint`` (taken at wall time ``wall``) proves no write since: every board is keyed by its
    content revision, or by a file stat last modified more than ``STAT_SETTLED_S`` before ``wall``. A stat key can
    miss a same-size commit inside the mtime tick of the previous one; a write after a settled stat lands in a later
    tick and moves it. (A board no current writer opened, e.g. ``boards/_archived``, has only its stat.)"""
    for entry in fingerprint:
        if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], tuple) and entry[1][:1] == ("stat",):
            for st in entry[1][1] if len(entry[1]) > 1 and isinstance(entry[1][1], tuple) else (None,):
                if st is not None and (not isinstance(st, tuple) or len(st) < 4 or st[3] / 1e9 > wall - STAT_SETTLED_S):
                    return False
    return True


def board_memo_key(path) -> tuple:
    """Observe same-stat commits before reusing per-board rows, independently of the counts TTL."""
    from hermes_cli import kanban_session_activity as _ksa

    key = _ksa._board_content_key(path)
    if key[:1] == ("stat",):
        # data_version is connection-local: retain identity to reject a replacement reader.
        try:
            with _ksa.cached_ro(path) as conn:
                return key, conn, conn.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.OperationalError as exc:
            # [daveey] The probe falls back to the stat exactly when the board is busy: a busy board has no key that
            # vouches for its memo row. The token matches no memo row and carries the error, so the reader reuses
            # that board's last good counts or re-raises it without spending a second busy budget; the other
            # boards' counts are never aborted (kanban-359833be53).
            if not is_busy(exc):
                raise
            return key, BusyToken(exc)
    return key


class BusyToken:
    """Ends a memo key whose board was busy: equal to nothing, so it never matches a memo row. [daveey]"""

    def __init__(self, error: sqlite3.OperationalError):
        self.error = error


def is_busy_key(key) -> bool:
    return isinstance(key, tuple) and len(key) == 2 and isinstance(key[1], BusyToken)


def counts_fresh(scope: tuple[str, ...], fingerprint: tuple, now: float) -> bool:
    """Caller holds ``_ksa._cache_lock``. The cached counts answer for ``fingerprint``: same scope and inputs, and
    either within the TTL or (``revalidatable`` fingerprint) younger than ``_ksa._COUNTS_MAX_AGE_S``."""
    from hermes_cli import kanban_session_activity as _ksa
    cache = _ksa._cache
    if cache is None or _ksa._cache_scope != scope or fingerprint != _ksa._cache_fingerprint:
        return False
    if getattr(force_read, "scope", None) == scope:
        return False
    if now < cache[0]:
        return True
    return _ksa._cache_revalidatable and now < _ksa._cache_read_at + _ksa._COUNTS_MAX_AGE_S


def attention_key(local: tuple | None = None, wall: float | None = None) -> tuple | None:
    """What the done checks read: the local boards' content keys (``local``, taken at ``wall``, when the caller
    already has them). None unless ``revalidatable``, or when the boards can't be read: then only the TTL serves the
    cache."""
    from hermes_cli import kanban_session_activity as _ksa
    if local is None:
        wall = time.time()
        try:
            local = _ksa._activity_fingerprint(include_shadows=False)
        except Exception:  # health: allow BLE001 -- a revalidation input; the recompute surfaces a real read error
            _log.debug("kanban attention key unavailable", exc_info=True)
            return None
    return local if revalidatable(local, time.time() if wall is None else wall) else None


def unchanged_inputs(scope: tuple[str, ...]) -> tuple[float, tuple] | None:
    """``(wall time, pinned fingerprints)`` when a read of ``scope`` now would be all cache hits: the counts answer
    for the boards' current content revisions (``counts_fresh``) and the attention counts (when checks are
    registered) for the local boards'. None otherwise, or while nothing is cached."""
    from hermes_cli import kanban_session_activity as _ksa
    with _ksa._cache_lock:
        _ksa._count_scopes.restore_locked(scope)
        if _ksa._cache is None or _ksa._cache_scope != scope:
            return None
    read_at = time.time()
    mono = time.monotonic()
    local = _ksa._activity_fingerprint(include_shadows=False)
    full = local + _ksa._shadow_fingerprint() + _ksa._origin_ledger_fingerprint()  # one board read for both keys
    with _ksa._cache_lock:
        if not counts_fresh(scope, full, mono):
            return None
    if _ksa._done_checks and _ksa._attention.get(scope, mono) is None:
        key = attention_key(local, read_at)
        if key is None or _ksa._attention.get(scope, mono, key) is None:
            return None
    return read_at, (local, full)


def refresh_if_unchanged(scope: tuple[str, ...], refresh) -> tuple | None:
    """Caller holds the annotation refresh lock. When nothing the read depends on moved, re-read inline, where every
    heavy part is a cache hit (no refresh thread, no recompute; the waiting sources still run and a new observation
    is dated): ``refresh(scope, read_at)``'s parts. None when an input moved (or the probe failed). Accounted as
    ``annotation.revalidate`` (loop_accounting): unchanged inputs are a skip, moved ones a key change."""
    from hermes_cli import loop_accounting
    with loop_accounting.pass_("annotation.revalidate") as acct:
        parts = _refresh_if_unchanged(scope, refresh)
        acct.change(parts is None)
        if parts is not None:
            acct.skip()
        return parts


def _refresh_if_unchanged(scope: tuple[str, ...], refresh) -> tuple | None:
    try:
        unchanged = unchanged_inputs(scope)
    except Exception:  # health: allow BLE001 -- only a shortcut probe; the bounded refresh reports a real read error
        _log.debug("kanban annotation input probe failed; refreshing", exc_info=True)
        return None
    if unchanged is None:
        return None
    read_at, fingerprints = unchanged
    inputs_pin.fingerprints = fingerprints
    try:
        return refresh(scope, read_at)
    finally:
        inputs_pin.fingerprints = None
