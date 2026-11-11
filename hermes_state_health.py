"""Profile-level session-storage health: one process-wide latch per state.db path.

A structurally corrupt state.db used to show up as an empty or partial Desktop sidebar,
green readiness and a 500 from ``/api/sessions``: every surface guessed on its own and
none said "the store is damaged" (#72046). This module is the single place that fact is
recorded. Writers (``SessionDB._halt_db_corrupt``), readers (``SessionDB`` read helpers)
and the readiness probe publish into it; ``gateway.readiness`` (``state_db`` /
``session_store`` checks, hence ``/api/status`` ``components.storage``) and the session
list endpoints read from it, so Desktop and readiness cannot disagree.

Only structural corruption latches: bare ``SQLITE_CORRUPT`` / ``SQLITE_NOTADB`` with no
FTS provenance. FTS-scoped damage has its own fail-open path with canonical rows intact,
and a malformed-schema row is healed by the web open path, so neither is reported here.

The latch never clears on its own. A corrupt image does not heal, and a store that
flickers between "ok" and "corrupt" is the silent failure this replaces. It resets when
the process restarts, which is the recovery boundary ``StateDbCorruptError`` already
documents (stop Hermes, recover or restore, start again). No marker is written to disk:
the file it would describe is the one that is damaged.

Because it never clears, it must not be set on one unconfirmed error (2026-11-03: one error on
a file that later passed ``quick_check`` refused every write for an hour; that later check shows
only that the pages read back consistent then, not that the file was never damaged). The writer
(``SessionDB._execute_write``) and the pooled reader first run a bounded ``quick_check`` of the
tables the failing statement touched, read-only in a fresh process
(``hermes_state_corruption_confirm``). Only a clean check allows ONE replay of the rolled-back
statement on the same connection, counted by ``note_unconfirmed_corruption``. Read timeout/no_budget
fails only the read and starts an unbounded background check; actual background damage latches.
Writer timeout, immediate damage/check errors and replay recurrence still latch. A COMMIT is never replayed (its settlement
is unknown). The latched reason names the SQLite code, operation, stage and check outcome.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Optional

from hermes_state_errors import (
    classify_persistence_error,
    is_fts_scoped_corruption_error,
    is_malformed_schema_error,
)

logger = logging.getLogger(__name__)

STORAGE_OK = "ok"
STORAGE_CORRUPT = "corrupt"

_lock = threading.Lock()
_corrupt: dict[str, str] = {}  # resolved db path -> first error text (log only, never served)
_unconfirmed: dict[str, int] = {}  # resolved db path -> structural errors a clean check did not confirm


def _key(db_path) -> str:
    return str(Path(db_path).expanduser().resolve(strict=False))


def is_structural_corruption_error(exc: BaseException) -> bool:
    """Canonical B-tree/schema/freelist damage: a corrupt/NOTADB error SQLite does not scope to
    the FTS index, and not the malformed-schema case the web open path repairs."""
    return (
        isinstance(exc, sqlite3.DatabaseError)
        and not is_fts_scoped_corruption_error(exc)
        and not is_malformed_schema_error(exc)
        and classify_persistence_error(exc) == "corrupt"
    )


def mark_storage_corrupt(db_path, reason: object) -> None:
    """Latch *db_path* as corrupt for the life of this process (idempotent)."""
    key = _key(db_path)
    with _lock:
        if key in _corrupt:
            return
        _corrupt[key] = str(reason)
    logger.error(
        "state.db at %s is structurally corrupt (%s); session storage is reported as corrupt "
        "until Hermes restarts on a recovered or restored file. Stop Hermes, then run "
        "`hermes sessions recover --source %s --inspect-only` or restore a snapshot.",
        db_path, reason, db_path,
    )
    try:
        from hermes_state_corruption_incident import schedule_incident

        schedule_incident(db_path, str(reason))
    except Exception:  # health: allow BLE001 -- incident reporting must not mask the original storage failure
        logger.exception("state.db corruption incident scheduling failed")


def note_storage_error(db_path, exc: BaseException, *, context: Optional[str] = None) -> bool:
    """Latch *db_path* when *exc* is structural corruption; True when it was. *context* (code,
    operation, replay outcome) is appended to the latched reason."""
    if getattr(exc, "hermes_read_corrupt_unconfirmed", False) or not is_structural_corruption_error(exc):
        return False
    mark_storage_corrupt(db_path, f"{exc} [{context}]" if context else exc)
    return True


def corruption_detail(exc: BaseException) -> str:
    """``sqlite=<name>(<code>)`` for a latch reason / log line: tells a misread from real damage
    later, when the logs from the moment have rotated and only the reason text is left."""
    name = getattr(exc, "sqlite_errorname", None) or "unknown"
    code = getattr(exc, "sqlite_errorcode", None)
    return f"sqlite={name}({code if code is not None else '?'})"


def write_op_name(fn: object) -> str:
    """The callback's qualified name, reduced to identifier characters (it lands in an error text
    that is bucketed by phrase, so nothing free-form may ride along)."""
    raw = getattr(fn, "__qualname__", None) or type(fn).__name__
    return "".join(ch for ch in str(raw) if ch.isalnum() or ch in "._<>")[:96] or "unknown"


def storage_state(db_path) -> str:
    """``"corrupt"`` once this process has seen structural corruption on *db_path*, else ``"ok"``."""
    with _lock:
        return STORAGE_CORRUPT if _key(db_path) in _corrupt else STORAGE_OK


def storage_corrupt_reason(db_path) -> Optional[str]:
    """The first error text latched for *db_path* (logs/diagnostics only), or None."""
    with _lock:
        return _corrupt.get(_key(db_path))


def note_unconfirmed_corruption(db_path) -> int:
    """Count one structural error that a scoped read-only ``quick_check`` did NOT confirm (the
    caller replays once instead of latching); returns this process's total for *db_path*."""
    key = _key(db_path)
    with _lock:
        _unconfirmed[key] = _unconfirmed.get(key, 0) + 1
        return _unconfirmed[key]


def unconfirmed_corruption_count(db_path) -> int:
    """How many structural errors on *db_path* this process saw but did not confirm."""
    with _lock:
        return _unconfirmed.get(_key(db_path), 0)


def reset_storage_state(db_path=None) -> None:
    """Forget the latch (and the unconfirmed-error count) for *db_path* (all paths when None).
    For tests and a verified in-process recovery; nothing in the runtime clears it on its own."""
    with _lock:
        if db_path is None:
            _corrupt.clear()
            _unconfirmed.clear()
        else:
            _corrupt.pop(_key(db_path), None)
            _unconfirmed.pop(_key(db_path), None)


__all__ = [
    "STORAGE_CORRUPT",
    "STORAGE_OK",
    "corruption_detail",
    "is_structural_corruption_error",
    "mark_storage_corrupt",
    "note_storage_error",
    "note_unconfirmed_corruption",
    "reset_storage_state",
    "storage_corrupt_reason",
    "storage_state",
    "unconfirmed_corruption_count",
    "write_op_name",
]
