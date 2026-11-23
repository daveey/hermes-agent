"""Bounded per-session message/counter source revisions. [daveey]

This supplements, never replaces, the listing/lineage cursor. Message edits,
deletes and moves cannot be inferred from MAX(messages.id); token/transcript
counters deliberately do not invalidate legacy session lists. Triggers on those
inputs maintain this independent clock inside the original writer transaction.
No message bodies are copied. REPLACE across unique keys is not supported (use
ON CONFLICT DO UPDATE so both old/new owners are marked).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass

from hermes_state_session_change import COUNTER_COLUMNS, FULL, _identity, _main_path, _quote

CHANGE_TABLE = "session_content_change"
REVISION_TABLE = "session_content_revision"
PREFIX = "session_content_on_"
INDEX = f"idx_{CHANGE_TABLE}_rev"
_COVERED = {}
_LOCK = threading.Lock()
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cursor:
    identity: tuple
    schema: int
    epoch: str
    revision: int


def _stamp(ref):
    return (f"UPDATE {REVISION_TABLE} SET revision=revision+1 WHERE id=1; "
            f"INSERT INTO {CHANGE_TABLE}(session_id,rev) SELECT {ref},revision "
            f"FROM {REVISION_TABLE} WHERE id=1 AND {ref} IS NOT NULL "
            "ON CONFLICT(session_id) DO UPDATE SET rev=excluded.rev;")


def wanted_triggers(conn):
    out = {}
    for table, key in (("sessions", "id"), ("messages", "session_id")):
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if key not in columns:
            continue
        watched = columns if table == "messages" else [c for c in columns if c in COUNTER_COLUMNS or c == key]
        for operation, ref in (("insert", "NEW"), ("delete", "OLD")):
            name = f"{PREFIX}{table}_{operation}"
            out[name] = (f"CREATE TRIGGER {name} AFTER {operation.upper()} ON {table} "
                         f"WHEN {ref}.{key} IS NOT NULL BEGIN {_stamp(f'{ref}.{key}')} END")
        changed = " OR ".join(f"OLD.{_quote(c)} IS NOT NEW.{_quote(c)}" for c in watched)
        name = f"{PREFIX}{table}_update"
        out[name] = (
            f"CREATE TRIGGER {name} AFTER UPDATE OF {','.join(_quote(c) for c in watched)} ON {table} "
            f"WHEN NEW.{key} IS NOT NULL AND ({changed}) BEGIN {_stamp(f'NEW.{key}')} END")
        name = f"{PREFIX}{table}_rekey"
        out[name] = (f"CREATE TRIGGER {name} AFTER UPDATE OF {key} ON {table} "
                     f"WHEN OLD.{key} IS NOT NULL AND OLD.{key} IS NOT NEW.{key} "
                     f"BEGIN {_stamp(f'OLD.{key}')} END")
    return out


def _installed(conn):
    return dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB ?",
                             (PREFIX + "*",)))


def _index_ready(conn):
    indexes = conn.execute(f"PRAGMA index_list({CHANGE_TABLE})").fetchall()
    return (any(r[1] == INDEX and not r[4] for r in indexes)
            and [r[2] for r in conn.execute(f"PRAGMA index_info({INDEX})")] == ["rev"])


def _has_storage(conn):
    return (len(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                             (CHANGE_TABLE, REVISION_TABLE)).fetchall()) == 2 and _index_ready(conn))


def ensure_installed(conn):
    """Writer-only O(1) install/repair; rotate epoch on every coverage repair."""
    wanted = wanted_triggers(conn)
    if not wanted:
        return False
    found = _installed(conn)
    if found == wanted and _has_storage(conn) and conn.execute(
            f"SELECT 1 FROM {REVISION_TABLE} WHERE id=1").fetchone():
        return False
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {CHANGE_TABLE} "
                     "(session_id TEXT PRIMARY KEY NOT NULL,rev INTEGER NOT NULL) WITHOUT ROWID")
        if not _index_ready(conn):
            conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
            conn.execute(f"CREATE INDEX {INDEX} ON {CHANGE_TABLE}(rev)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {REVISION_TABLE} "
                     "(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,epoch TEXT NOT NULL)")
        conn.execute(f"INSERT OR IGNORE INTO {REVISION_TABLE} VALUES(1,0,'')")
        conn.execute(f"UPDATE {REVISION_TABLE} SET epoch=? WHERE id=1", (uuid.uuid4().hex,))
        for name in set(found) | set(wanted):
            if found.get(name) != wanted.get(name):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                if name in wanted:
                    conn.execute(wanted[name])
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return True


def install_fail_open(conn):
    try:
        ensure_installed(conn)
        return True
    except sqlite3.Error as exc:
        log.warning("session content cursor unavailable; capture requires full recovery: %s", exc)
        return False


def _covered(conn, key):
    with _LOCK:
        if key in _COVERED:
            return _COVERED[key]
    installed = _installed(conn)
    covered = _has_storage(conn) and bool(installed) and installed == wanted_triggers(conn)
    with _LOCK:
        if len(_COVERED) >= 64:
            _COVERED.clear()
        _COVERED[key] = covered
    return covered


def _read(conn, path, identity, cursor, limit):
    schema = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    if not _covered(conn, (path, identity, schema)):
        return FULL, None
    state = conn.execute(f"SELECT revision,epoch,(SELECT MAX(rev) FROM {CHANGE_TABLE}) "
                         f"FROM {REVISION_TABLE} WHERE id=1").fetchone()
    if state is None or int(state[0]) != int(state[2] or 0):
        return FULL, None
    fresh = Cursor(identity, schema, state[1], int(state[0]))
    if (cursor is None or (cursor.identity, cursor.schema, cursor.epoch) != (identity, schema, fresh.epoch)
            or fresh.revision < cursor.revision):
        return FULL, fresh
    ids = frozenset(row[0] for row in conn.execute(
        f"SELECT session_id FROM {CHANGE_TABLE} WHERE rev>? AND rev<=? LIMIT ?",
        (cursor.revision, fresh.revision, limit + 1)))
    return (FULL if len(ids) > limit else ids), fresh


def changed_since(conn, cursor, *, limit=2000):
    """A FULL is not an empty delta. The caller installs cursors only after publication."""
    if not 1 <= limit <= 2000:
        raise ValueError("limit must be between 1 and 2000")
    path = _main_path(conn)
    identity = _identity(path)
    if identity is None:
        return FULL, None
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        result = _read(conn, path, identity, cursor, limit)
    finally:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
    return result if _identity(path) == identity else (FULL, None)
