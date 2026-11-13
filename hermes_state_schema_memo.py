"""[daveey] Process-level memo of a settled ``SessionDB._init_schema`` (design t_01a1d45d75851804 §2d).

Every short-lived writable ``SessionDB`` used to rerun the whole schema init: ``SCHEMA_SQL``, the column
reconcile, the PK heals and a full ``sessions`` scan for stray delegate markers (~6.9% of a steady serve's
samples on the hub). Once one open in this process has run the full init on a store and left it settled,
later opens of the SAME file at the SAME schema only restore the per-instance flags that init set.

The memo key is the file identity stat'd after connecting (realpath, st_dev, st_ino), SQLite's
``PRAGMA schema_version`` (bumped by every DDL, from any process), the ``schema_version`` table value
(data-migration level), ``store_instance_id`` and a digest of this build's schema code. A hit also
re-probes the two FTS tables (``LIMIT 0``) and must agree with the memoized flags. Any change, a
replaced or quarantined file, or a store not yet settled (stale FTS, an unfinished budgeted backfill or
repair) reruns the full init. ``fts_stale`` is read on every open; set by another handle, it forces the
full init, whose stale-FTS path is the one that must run.

The delegate-marker heal is data repair, not schema: it runs with the full init (the first open per key
per process) and otherwise at most once per ``HEAL_INTERVAL_S`` per store, from the memo-hit open.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("hermes_state")

HEAL_INTERVAL_S = 600.0
# Always restored on a hit, whatever the first init left them at; other changed scalars are found by diff.
_ALWAYS_RESTORED = ("_fts_enabled", "_trigram_available", "_fts_stale")
_SIMPLE = (bool, int, float, str, bytes, type(None))

_lock = threading.Lock()
_memo: dict[tuple, dict[str, Any]] = {}  # key -> attributes the full init set on self
_last_heal: dict[str, float] = {}  # realpath -> monotonic time of the last delegate-marker heal
_code_digest: Optional[str] = None


def clear() -> None:
    """Forget every memoized store (tests; a deliberate in-process reinit)."""
    global _code_digest
    with _lock:
        _memo.clear()
        _last_heal.clear()
        _code_digest = None


def _schema_code_digest() -> str:
    global _code_digest
    if _code_digest is None:
        import hermes_state_common as common
        import hermes_state_schema as schema

        h = hashlib.sha256()
        for part in (common.SCHEMA_SQL, common.DEFERRED_INDEX_SQL, common.FTS_SQL, common.FTS_TRIGRAM_SQL,
                     str(common.SCHEMA_VERSION), str(common.FTS_STORAGE_VERSION)):
            h.update(part.encode("utf-8", "surrogatepass"))
            h.update(b"\0")
        try:
            with open(schema.__file__, "rb") as f:  # the migration code itself
                h.update(f.read())
        except OSError:
            pass
        _code_digest = h.hexdigest()
    return _code_digest


def _settled_meta_keys() -> tuple[str, ...]:
    from hermes_state_common import FTS_STALE_KEY
    from hermes_state_legacy_refusal import LEGACY_REFUSAL_REPAIR_DONE
    from hermes_state_schema import _MESSAGE_UID_BACKFILL_DONE
    return (FTS_STALE_KEY, "store_instance_id", LEGACY_REFUSAL_REPAIR_DONE, _MESSAGE_UID_BACKFILL_DONE)


def _probe(db_path, conn: sqlite3.Connection) -> Optional[tuple[tuple, dict[str, Any]]]:
    """``(key, state_meta subset)`` for the open file, or None when it cannot be read cheaply."""
    try:
        real = os.path.realpath(db_path)
        st = os.stat(real)
        pragma_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        keys = _settled_meta_keys()
        marks = ", ".join("?" for _ in keys)
        meta = {r[0]: r[1] for r in conn.execute(
            f"SELECT key, value FROM state_meta WHERE key IN ({marks})", keys).fetchall()}
    except (OSError, sqlite3.Error):
        return None
    key = (real, st.st_dev, st.st_ino, pragma_version, None if row is None else row[0],
           meta.get("store_instance_id"), _schema_code_digest(),
           type(conn))  # the connection class decides what this runtime's SQLite can do (tests swap it)
    return key, meta


def _settled(meta: dict[str, Any]) -> bool:
    from hermes_state_common import FTS_STALE_KEY
    # store_instance_id is part of the key, not required: stores created before #97568 lack it.
    return FTS_STALE_KEY not in meta and all(k in meta for k in _settled_meta_keys()[2:])


def snapshot(db) -> dict[str, Any]:
    """Instance attributes before the full init, to diff against afterwards."""
    return dict(vars(db))


def record(db, before: dict[str, Any]) -> None:
    """Remember a successful full init when it left the store settled at this build's schema."""
    probed = _probe(db.db_path, db._conn)
    if probed is None:
        return
    key, meta = probed
    from hermes_state_common import SCHEMA_VERSION
    if not _settled(meta) or key[4] != SCHEMA_VERSION or db._conn.in_transaction:
        return
    after = vars(db)
    changed = {name: value for name, value in after.items()
               if name in _ALWAYS_RESTORED or before.get(name, _MISSING) is not value}
    if not all(isinstance(v, _SIMPLE) for v in changed.values()):
        return  # an init that set a mutable object on self is not safely replayable
    with _lock:
        _memo[key] = changed
        _last_heal[key[0]] = time.monotonic()  # the full init just ran the heal


_MISSING = object()


def try_restore(db) -> bool:
    """On a settled, unchanged store already initialized in this process, restore its flags and
    return True (the caller skips the full init); otherwise False."""
    probed = _probe(db.db_path, db._conn)
    if probed is None:
        return False
    key, meta = probed
    if not _settled(meta):
        return False
    with _lock:
        cached = _memo.get(key)
    if cached is None:
        return False
    # Re-probe the FTS tables (LIMIT 0 reads): a runtime without the trigram tokenizer or FTS5, or an
    # index dropped without DDL visible here, must not inherit another open's flags. A lock or any
    # other error falls back to the full init, whose lock patience owns the wait.
    cursor = db._conn.cursor()
    try:
        fts = db._fts_table_probe(cursor, "messages_fts") is True
        trigram = fts and db._fts_table_probe(cursor, "messages_fts_trigram") is True
    except (sqlite3.Error, UnicodeDecodeError):
        return False
    if fts != cached.get("_fts_enabled") or trigram != cached.get("_trigram_available"):
        return False
    for name, value in cached.items():
        setattr(db, name, value)
    _maybe_heal(db, key[0])
    return True


def _maybe_heal(db, real: str) -> None:
    now = time.monotonic()
    with _lock:
        last = _last_heal.get(real)
        if last is not None and now - last < HEAL_INTERVAL_S:
            return
        _last_heal[real] = now  # claimed before running: concurrent openers never double-scan
    try:
        db._heal_reset_rows_carrying_a_delegate_marker(db._conn.cursor())
    except sqlite3.Error as exc:  # a data repair must never fail an open; the next interval retries
        logger.debug("throttled delegate-marker heal skipped: %s", exc)
