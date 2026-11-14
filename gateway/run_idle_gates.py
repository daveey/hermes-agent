"""Idle gates for the gateway's per-profile pollers (heartbeat restore, handoff watcher, loop wakeup).

Entering ``_profile_runtime_scope`` costs a config.yaml load, a ``.env`` parse, secret hydration and a
terminal-policy build; on a multiplex gateway the pollers paid that per profile per tick with nothing
to do. Each gate answers "does this profile's store hold work?" from the goals-cached SessionDB with
ONLY the HERMES_HOME contextvar installed. Every gate fails OPEN: an unavailable store, a failing
read or a corrupt row is "cannot prove emptiness", never "idle".

The heartbeat/loop answers are ``state_meta`` prefix scans: ``LIKE ... ESCAPE`` cannot use the key
index, so each tick walked the whole table and re-parsed every retained ``cleared``/``paused`` row.
A proven-EMPTY answer is reused while the reader connection's committed revision (``_revision``) is
unchanged. Only "no active row" is remembered: active work, unparseable rows and read errors are
re-examined on every tick as before. Activity is status-only (no clock), so an unchanged store cannot
become active without a commit.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("gateway.run")

# (kind, db, conn) -> the revision whose scan proved the store empty. Strong refs (sqlite3.Connection
# is not weak-referenceable), bounded like the other per-connection revision caches; a closed
# connection raises on its next PRAGMA and can never match a live one.
_EMPTY_PROOFS: OrderedDict[tuple, tuple] = OrderedDict()
_EMPTY_PROOFS_LOCK = threading.Lock()
_MAX_EMPTY_PROOFS = 64


def _profile_session_db_probe(profile_home: Path) -> Optional[Any]:
    """The goals-cached SessionDB for *profile_home*; None when unavailable."""
    from hermes_cli.goals import _get_session_db
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(profile_home))
    try:
        return _get_session_db()
    except Exception:
        logger.debug("session-db probe failed for %s", profile_home, exc_info=True)
        return None
    finally:
        reset_hermes_home_override(token)


def _revision(conn) -> tuple:
    # data_version moves on every commit by ANOTHER connection (pooled readers never write);
    # total_changes covers the writer-connection fallback's own writes.
    return conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes


class _ConnMeta:
    """``SessionDB.list_meta_prefix`` on one checked-out connection, so the scan runs between the two
    revision reads of the connection that vouches for it."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def list_meta_prefix(self, prefix: str) -> list[tuple[str, str]]:
        # A primary-key range, not ``LIKE ? ESCAPE``: that form cannot use the index and reads every
        # state_meta value (244 MB on one gateway store) on each probe. The filter keeps the match literal.
        if not prefix:
            return []
        rows = self._conn.execute("SELECT key, value FROM state_meta WHERE key >= ? AND key < ?",
                                  (prefix, prefix[:-1] + chr(min(ord(prefix[-1]) + 1, 0x10FFFF))))
        return [(row[0], row[1]) for row in rows if row[0].startswith(prefix)]


def _store_has_work_cached(db: Any, kind: str, store_has_work: Callable[[Any], bool]) -> bool:
    """``store_has_work`` over ``state_meta``, skipping the scan while a proven-empty answer's
    revision still holds. Read errors propagate and are never remembered."""
    read = getattr(db, "_read_retrying_ioerr", None)
    if not callable(read):  # not a SessionDB: no revision to key on
        return store_has_work(db)

    def probe(conn) -> bool:
        identity = (kind, db, conn)
        if conn.in_transaction:
            # Writer fallback inside an open transaction: uncommitted rows (and a rollback, which
            # total_changes does not undo) must never become a proof.
            with _EMPTY_PROOFS_LOCK:
                _EMPTY_PROOFS.pop(identity, None)
            return store_has_work(_ConnMeta(conn))
        before = _revision(conn)
        with _EMPTY_PROOFS_LOCK:
            if _EMPTY_PROOFS.get(identity) == before:
                _EMPTY_PROOFS.move_to_end(identity)
                return False
            _EMPTY_PROOFS.pop(identity, None)
        has_work = store_has_work(_ConnMeta(conn))
        # Keyed by the revision read BEFORE the scan: a commit landing during the scan moves the
        # revision past ``before``, so a proof the scan may have missed it under can never match.
        if not has_work:
            with _EMPTY_PROOFS_LOCK:
                _EMPTY_PROOFS[identity] = before
                while len(_EMPTY_PROOFS) > _MAX_EMPTY_PROOFS:
                    _EMPTY_PROOFS.popitem(last=False)
        return has_work

    return bool(read(probe))


def _gate(profile_home: Path, store_has_work: Callable[[Any], bool], kind: Optional[str] = None) -> bool:
    db = _profile_session_db_probe(profile_home)
    if db is None:
        return True
    try:
        if kind is None:
            return store_has_work(db)
        return _store_has_work_cached(db, kind, store_has_work)
    except Exception:
        logger.debug("idle probe failed for %s; keeping the full sweep", profile_home, exc_info=True)
        return True


def profile_has_active_heartbeat(profile_home: Path) -> bool:
    from hermes_cli import heartbeat

    return _gate(profile_home, heartbeat.store_has_active_heartbeat, "heartbeat")


def profile_has_active_loop(profile_home: Path) -> bool:
    from hermes_cli import loops

    return _gate(profile_home, loops.store_has_active_loop, "loop")


def _quiet_store_has_active_loop(db: Any) -> bool:
    from hermes_cli import loops

    return loops.store_has_active_loop(db, warn=False)


def current_store_may_have_active_loop() -> bool:
    """The loop gate for the store the CURRENT scope resolves (the loop watcher's unscoped root scan).
    False only when that store is proven to hold no active loop; an unavailable store or failing read
    is True, leaving ``list_active_loops`` its own ``[]``-on-error handling."""
    from hermes_cli.goals import _get_session_db

    db = _get_session_db()
    if db is None:
        return True
    try:
        # Quiet: before this gate the root scan only ran list_active_loops, which skips an unparseable
        # row silently; a corrupt row is never cached, so a warning here would repeat every tick.
        return _store_has_work_cached(db, "loop", _quiet_store_has_active_loop)
    except Exception:
        logger.debug("idle loop probe failed; keeping the full scan", exc_info=True)
        return True


def profile_has_pending_handoff(profile_home: Path) -> bool:
    return _gate(profile_home, lambda db: db.has_pending_handoffs())


async def off_loop_gate(runner: object, probe: Callable[[], bool]) -> bool:
    """Run a sync gate through the runner's executor hop. Runners without one (bare test stand-ins
    for the handoff watcher) keep the historical always-enter behaviour."""
    offload = getattr(runner, "_run_in_executor_with_context", None)
    if not callable(offload):
        return True
    return bool(await offload(probe))
