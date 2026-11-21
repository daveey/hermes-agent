"""Bounded, nonsecret turn-lease custody events (investigation tooling) [daveey t_01a1f9c3ea7cb7fa].

A running turn was refused with "missing lease row" seconds after admission and no log said which
statement removed the row, or whether it was ever readable. Every statement that inserts, deletes or
refuses on ``session_turn_leases`` reports here, so the next occurrence names its producer:

- ``phase=txn`` lines are emitted INSIDE the write transaction. They are not proof of commit.
- ``phase=committed`` / ``phase=rolled_back`` settle the events of one ``_execute_write`` attempt,
  matched by ``txn=``. Events with no settlement line ran in a transaction this module cannot see.
- ``phase=admission`` is the reader-connection readback right after acquisition.

Observation only: nothing here inserts, renews or restores a lease row, and a logging failure never
changes an admission or a refusal. Keys, holders, session ids and paths appear only as 12-hex SHA-256
tags (``tag(value)``), so a line can be matched to a known id without exposing a holder capability.
"""
import hashlib
import logging
import os
import sqlite3
import sys
import threading
from typing import NoReturn

_LOG = logging.getLogger("hermes_state.lease_custody")
_STATE = threading.local()
_QUIET_WHEN_ZERO = frozenset({"sweep", "release", "worker_release", "worker_cleanup"})


def tag(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:12] if value else "-"


def _txn() -> str:
    return f"{os.getpid()}.{threading.get_ident() & 0xFFFFFF:x}.{getattr(_STATE, 'serial', 0)}"




def _caller() -> str:
    """Bounded code locations only; never inspect locals, arguments or source text."""
    frame = sys._getframe(1)
    locations = []
    try:
        for _ in range(12):
            if frame is None:
                break
            code = frame.f_code
            # Strip directory names and restrict log delimiters, including dynamic code names.
            filename = os.path.basename(code.co_filename)
            if filename != "hermes_state_lease_custody.py":
                def clean(s):
                    return "".join(c if c.isascii() and (c.isalnum() or c in "._-") else "_" for c in s[:80])
                locations.append(f"{clean(filename)}:{clean(code.co_name)}:{frame.f_lineno}")
            frame = frame.f_back
        return ">".join(locations) or "-"
    finally:
        del frame


def observe(op: str, key, holder, *, store=None, changed=None, row_holder=None, session_id=None, why=None) -> None:
    """One custody event from inside a transaction. ``changed`` is the statement's SQL rowcount."""
    try:
        if changed == 0 and op in _QUIET_WHEN_ZERO:
            return  # a no-op delete removed nothing: not a producer, and release is idempotent
        _STATE.pending = getattr(_STATE, "pending", 0) + 1
        level = logging.WARNING if op in ("missing", "mismatch") else logging.INFO
        _LOG.log(
            level,
            "turn_lease_custody phase=txn op=%s txn=%s store=%s key=%s session=%s root_is_session=%s "
            "holder=%s row_holder=%s changed=%s why=%s sqlite=%s caller=%s",
            op, _txn(), tag(store), tag(key), tag(session_id),
            "-" if session_id is None else int(session_id == key),
            tag(holder), tag(row_holder), changed if type(changed) is int else "-", why or "-",
            sqlite3.sqlite_version, _caller(),
        )
    except Exception:  # health: allow BLE001 -- optional diagnostics cannot change fencing
        return


def settle(outcome: str) -> None:
    """Close this thread's write attempt: say whether the events above committed or rolled back."""
    try:
        pending = getattr(_STATE, "pending", 0)
        if pending:
            _LOG.info("turn_lease_custody phase=%s txn=%s events=%d", outcome, _txn(), pending)
        _STATE.pending = 0
        _STATE.serial = getattr(_STATE, "serial", 0) + 1
    except Exception:  # health: allow BLE001 -- optional diagnostics cannot change fencing
        return


def delete_row(conn, op: str, key, holder, *, store=None, session_id=None, why=None) -> int:
    """The existing holder-qualified DELETE, with its real rowcount reported."""
    changed = conn.execute("DELETE FROM session_turn_leases WHERE conversation_id = ? AND holder = ?",
                           (key, holder)).rowcount
    observe(op, key, None, store=store, row_holder=holder, changed=changed, session_id=session_id, why=why)
    return changed


def sweep(conn, cutoff: float, store=None) -> None:
    """The existing long-expired sweep, naming (as tags) up to eight of the rows it removed."""
    rows = conn.execute("SELECT conversation_id, holder FROM session_turn_leases WHERE expires_at < ? LIMIT 8",
                        (cutoff,)).fetchall()
    changed = conn.execute("DELETE FROM session_turn_leases WHERE expires_at < ?", (cutoff,)).rowcount
    for row in rows:
        observe("sweep", row[0], None, store=store, row_holder=row[1], changed=changed)
    if changed and not rows:
        observe("sweep", None, None, store=store, changed=changed)


def refuse_lost(conn, session_id, key, holder, lease, store=None) -> NoReturn:
    """Report a holder's refused write, then raise the same refusal as before."""
    from hermes_state_errors import SessionTurnLeaseLostError
    reason = "missing lease row" if lease is None else "holder mismatch"
    try:
        rows = conn.execute("SELECT COUNT(*) FROM session_turn_leases").fetchone()[0]
        exists = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        why = f"lease_rows={rows},session_row={int(exists is not None)},parent={int(bool(exists and exists[0]))}"
    except sqlite3.Error:
        why = "context_unreadable"
    observe("missing" if lease is None else "mismatch", key, holder, store=store, session_id=session_id,
            row_holder=lease["holder"] if lease is not None else None, why=why)
    raise SessionTurnLeaseLostError(
        f"Session turn lease lost; refusing transcript write for {session_id!r} ({reason})")


def admission_readback(db, session_id, holder, committed) -> None:
    """After acquisition returned True: was the row readable on the READER connection?"""
    try:
        if committed is None:
            _LOG.warning(
                "turn_lease_custody phase=admission op=readback_absent pid=%d store=%s session=%s holder=%s sqlite=%s",
                os.getpid(), tag(getattr(db, "db_path", None)), tag(session_id), tag(holder), sqlite3.sqlite_version)
    except Exception:  # health: allow BLE001 -- optional diagnostics cannot change fencing
        return
