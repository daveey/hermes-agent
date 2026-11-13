"""[daveey] Per-session change cursor for state.db (design t_01a1d563324d7e8e §2 P3, card D1).

The session list, sidebar and project tree re-rank every session on each poll because state.db has no
sessions revision: ``data_version`` moves on any write (140 of 300 two-second windows on the hub), so
a reader cannot tell "nothing a listing reads moved" from "a counter ticked". This module adds one:

- ``session_change(session_id PRIMARY KEY, rev)``: the revision at which each session last changed.
- ``session_change_revision``: one row, ``revision`` (the newest ``rev``) and an install ``epoch``.

Triggers feed both, in the writer's own transaction (read-your-writes). Coverage is an EXCLUSION list
(review C8): every ``sessions`` column bumps except the pure counters in ``COUNTER_COLUMNS``, so a
column added later is watched once the next writer init regenerates the triggers. Included therefore:
``last_activity_at`` (raised mid-turn before any message, ``touch_session_activity``), ``started_at``,
``ended_at``, ``end_reason``, ``parent_session_id``, ``title``, ``pinned``, ``hidden``, the archive flags,
the workspace columns, ``source`` and ``model_config`` (its JSON carries the ``_branched_from`` /
``_delegate_from`` / ``_reset_from`` lineage markers). ``message_count`` bumps only when it crosses
0 (a launch stub becoming real, or a reset to empty). INSERT (including ``INSERT OR REPLACE``) and
DELETE always bump. A re-keyed row moves its old id too.

Superseded segments: a local-lineage receipt in ``state_meta`` (``gateway.local_policy.v1:*`` naming
more than one segment, ``LOCAL_LINEAGE_PREDICATE_SQL``, read by ``exclude_superseded_segments``) changes
which rows a listing shows. Receipts are rare (a local reset) and sit in a partial index, so readers
digest them into the cursor rather than trigger on ``state_meta`` (every message insert upserts a
``state_meta`` row); a cursor across a receipt change answers FULL. A changed id that is a lineage segment
(its ``end_reason`` moved) can change which sibling segment is the head: callers recompute the lineage
index (``_cached_local_lineage_index`` already does on any commit).

Messages are NOT covered: a reader that ranks by message time keeps its own ``messages.id`` watermark.

Every trigger body bumps ``revision`` and upserts exactly one non-NULL id at it, so
``MAX(session_change.rev) == revision`` holds for every write that went through the triggers. Bodies are
kept minimal on purpose: SQLite compiles trigger programs into each statement that can fire them, and the
writer re-prepares its statements per write, so the hot message path pays their size on every insert.

``changed_since(conn, cursor)`` trusts the cursor only when all of these hold, else it answers ``FULL``:

- (a) ``MAX(rev) == revision`` in the same snapshot;
- (b) ``revision >= cursor.revision`` (an in-place restore regresses it);
- (c) the file's (dev, ino), its ``-shm`` identity, ``PRAGMA schema_version``, the ``schema_version``
  table value and the install ``epoch`` all match the cursor, and the installed triggers are exactly the
  ones this build generates from the live columns;
- the local-lineage receipt digest matches the cursor (no segment superseded since).

Mixed writers: an older build does not know these triggers and never drops them (state.db drops only
allowlisted FTS names), so its writes still fire them. A store whose triggers are missing or stale
(rebuilt or copied by an older build, a column added by a newer one) fails (c), and every reader falls
back to FULL until a current writer reinstalls them; a (re)install rotates the epoch, so a cursor from
before the gap is never trusted afterwards.

The install is O(1): two empty tables, one index and the triggers; no backfill. A fresh install, or a
caller with no cursor, starts at FULL.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Optional, Union

logger = logging.getLogger("hermes_state")

CHANGE_TABLE = "session_change"
REVISION_TABLE = "session_change_revision"
PREFIX = "session_change_on_"
# Pure counters and accumulators: an update that changes only these is not a session-list change. Every
# other ``sessions`` column (including ones added after this list was written) bumps.
COUNTER_COLUMNS = frozenset({
    "message_count",  # bumps on its own trigger, only when it crosses 0
    "tool_call_count", "api_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
    "transcript_revision", "compression_fallback_streak", "compression_ineffective_count",
    "compression_overload_streak",
})

_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {CHANGE_TABLE} "
    "(session_id TEXT PRIMARY KEY NOT NULL, rev INTEGER NOT NULL) WITHOUT ROWID",
    f"CREATE INDEX IF NOT EXISTS idx_{CHANGE_TABLE}_rev ON {CHANGE_TABLE}(rev)",
    f"CREATE TABLE IF NOT EXISTS {REVISION_TABLE} "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL, epoch TEXT NOT NULL)",
)


class _Full:
    __slots__ = ()

    def __repr__(self) -> str:
        return "FULL"


FULL = _Full()  # the caller must do its full read


@dataclass(frozen=True)
class Cursor:
    dev: int
    ino: int
    shm: Optional[tuple]
    schema_cookie: int
    schema_level: Optional[int]
    epoch: str
    lineage: str
    revision: int


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# Trigger programs are compiled into every statement they can fire for, and the writer re-prepares its
# statements on each write attempt (its scoped authorizer expires them), so bodies stay as small as
# possible: the hot message path pays their compile cost on every insert.
def _stamp(ref: str) -> str:
    """Bump the revision and stamp ``ref`` (an id expression the trigger's WHEN proved non-NULL) at it."""
    return (f"UPDATE {REVISION_TABLE} SET revision = revision + 1 WHERE id = 1; "
            f"INSERT INTO {CHANGE_TABLE} (session_id, rev) SELECT {ref}, revision FROM {REVISION_TABLE} "
            "WHERE id = 1 ON CONFLICT(session_id) DO UPDATE SET rev = excluded.rev;")


def _lineage_digest(conn: sqlite3.Connection) -> str:
    """Digest of the local-lineage receipts that can supersede a segment. Read through the partial index
    ``idx_state_meta_local_lineage`` (the predicate must be the literal one); there are few receipts, one
    per local reset. Read instead of triggered: every message insert upserts ``state_meta`` (the projection
    generation), and a ``state_meta`` trigger would be compiled into each of those statements."""
    from hermes_state_common import LOCAL_LINEAGE_PREDICATE_SQL
    h = hashlib.blake2b(digest_size=16)
    for key, value in conn.execute(
            f"SELECT key, value FROM state_meta WHERE {LOCAL_LINEAGE_PREDICATE_SQL} ORDER BY key"):
        key, value = str(key), str(value)
        h.update(f"{len(key)}:{key}{len(value)}:{value}".encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def wanted_triggers(conn: sqlite3.Connection) -> dict[str, str]:
    """Trigger name -> CREATE statement, generated from the live ``sessions`` columns."""
    columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    if not columns:
        return {}
    out = {
        f"{PREFIX}sessions_insert": f"CREATE TRIGGER {PREFIX}sessions_insert AFTER INSERT ON sessions "
                                    f"WHEN NEW.id IS NOT NULL BEGIN {_stamp('NEW.id')} END",
        f"{PREFIX}sessions_delete": f"CREATE TRIGGER {PREFIX}sessions_delete AFTER DELETE ON sessions "
                                    f"WHEN OLD.id IS NOT NULL BEGIN {_stamp('OLD.id')} END",
    }
    watched = [c for c in columns if c not in COUNTER_COLUMNS]
    changed = " OR ".join(f"OLD.{_quote(c)} IS NOT NEW.{_quote(c)}" for c in watched)
    out[f"{PREFIX}sessions_update"] = (
        f"CREATE TRIGGER {PREFIX}sessions_update AFTER UPDATE OF {', '.join(_quote(c) for c in watched)} "
        f"ON sessions WHEN NEW.id IS NOT NULL AND ({changed}) BEGIN {_stamp('NEW.id')} END")
    # A re-keyed row also moves its old id (the update trigger above stamps the new one).
    out[f"{PREFIX}sessions_rekey"] = (
        f"CREATE TRIGGER {PREFIX}sessions_rekey AFTER UPDATE OF id ON sessions "
        f"WHEN OLD.id IS NOT NULL AND OLD.id IS NOT NEW.id BEGIN {_stamp('OLD.id')} END")
    if "message_count" in columns:
        # The hot path (every message insert bumps the counter): one cheap WHEN, a body only at 0 <-> non-0.
        out[f"{PREFIX}sessions_message_count"] = (
            f"CREATE TRIGGER {PREFIX}sessions_message_count AFTER UPDATE OF message_count ON sessions "
            "WHEN NEW.id IS NOT NULL AND "
            "(COALESCE(OLD.message_count, 0) = 0) IS NOT (COALESCE(NEW.message_count, 0) = 0) "
            f"BEGIN {_stamp('NEW.id')} END")
    return out


def _installed(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                             (PREFIX + "*",)))


def _has_tables(conn: sqlite3.Connection) -> bool:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
                                        (CHANGE_TABLE, REVISION_TABLE))}
    return names == {CHANGE_TABLE, REVISION_TABLE}


def ensure_installed(conn: sqlite3.Connection) -> bool:
    """Install or repair the tables and triggers. Reads only on the settled path; a (re)install rotates
    the epoch so no cursor issued before it is trusted. O(1): no backfill. Returns whether it wrote."""
    wanted = wanted_triggers(conn)
    if not wanted:
        return False
    found = _installed(conn)
    if found == wanted and _has_tables(conn) and conn.execute(
            f"SELECT 1 FROM {REVISION_TABLE} WHERE id = 1").fetchone():
        return False
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for ddl in _TABLE_DDL:
            conn.execute(ddl)
        conn.execute(f"INSERT OR IGNORE INTO {REVISION_TABLE} (id, revision, epoch) VALUES (1, 0, '')")
        conn.execute(f"UPDATE {REVISION_TABLE} SET epoch = ? WHERE id = 1", (uuid.uuid4().hex,))
        for name in set(found) - set(wanted):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name, sql in wanted.items():
            if found.get(name) != sql:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(sql)
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return True


def install_fail_open(conn: sqlite3.Connection) -> None:
    """``ensure_installed`` for the writer init: a failure (a lock, a read-only file) never fails the open;
    readers answer FULL until a later init installs the triggers."""
    try:
        ensure_installed(conn)
    except sqlite3.Error as exc:
        logger.warning("session change cursor not installed (readers fall back to full reads): %s", exc)


def drop_triggers(conn: sqlite3.Connection) -> None:
    """Remove the triggers (not the tables), e.g. before ``DROP COLUMN`` of a watched column: the update
    trigger names each one. Readers answer FULL until the next writer init reinstalls them."""
    for name in list(_installed(conn)):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def _main_path(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ""
    return ""


def _identity(path: str) -> Optional[tuple]:
    """``(dev, ino, shm)`` of the database file and its ``-shm``; None when the file can't be stat'd."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        sh = os.stat(path + "-shm")
        shm = (sh.st_dev, sh.st_ino)
    except OSError:
        shm = None
    return (st.st_dev, st.st_ino, shm)


# (path, dev, ino, schema cookie) -> whether the installed triggers are exactly the generated ones. Any DDL
# (a trigger dropped or recreated, a column added) moves the cookie, so a hit proves the same coverage.
_COVERED: dict[tuple, bool] = {}
_COVERED_LOCK = threading.Lock()
_COVERED_MAX = 64


def _covered(conn: sqlite3.Connection, key: tuple) -> bool:
    with _COVERED_LOCK:
        hit = _COVERED.get(key)
    if hit is not None:
        return hit
    value = _has_tables(conn) and bool(_installed(conn)) and _installed(conn) == wanted_triggers(conn)
    with _COVERED_LOCK:
        if len(_COVERED) >= _COVERED_MAX:
            _COVERED.clear()
        _COVERED[key] = value
    return value


def _schema_level(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row and row[0] is not None else None


def changed_since(conn: sqlite3.Connection, cursor: Optional[Cursor], *,
                  limit: Optional[int] = None) -> tuple[frozenset | _Full, Optional[Cursor]]:
    """``(session ids changed after cursor, cursor')`` or ``(FULL, cursor')``.

    ``cursor'`` is None when the store cannot vouch for changes right now (missing or stale triggers, a
    broken invariant), so the next call is FULL too. ``limit``: answer FULL rather than return more ids.
    A deleted session's id is returned like any other; the caller treats a missing row as a delete.
    Raises ``sqlite3.Error`` like any read."""
    path = _main_path(conn)
    before = _identity(path)
    if before is None:
        return FULL, None  # in-memory or vanished file: no identity to bind a cursor to
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")  # one snapshot for the invariant, the coverage check and the ids
    try:
        fresh, ids = _read(conn, path, before, cursor, limit)
    finally:
        if own and conn.in_transaction:
            conn.execute("COMMIT")
    if fresh is None or _identity(path) != before:
        return FULL, None
    if ids is None or (limit is not None and len(ids) > limit):
        return FULL, fresh
    return ids, fresh


def _read(conn: sqlite3.Connection, path: str, ident: tuple, cursor: Optional[Cursor],
          limit: Optional[int]) -> tuple[Optional[Cursor], Optional[frozenset]]:
    cookie = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    if not _covered(conn, (path, ident[0], ident[1], cookie)):
        return None, None
    state = conn.execute(f"SELECT r.revision, r.epoch, (SELECT MAX(rev) FROM {CHANGE_TABLE}) "
                         f"FROM {REVISION_TABLE} r WHERE r.id = 1").fetchone()
    if state is None or int(state[2] or 0) != int(state[0]):  # (a)
        return None, None
    fresh = Cursor(ident[0], ident[1], ident[2], cookie, _schema_level(conn), str(state[1]),
                   _lineage_digest(conn), int(state[0]))
    if (cursor is None or fresh.revision < cursor.revision  # (b)
            or (cursor.dev, cursor.ino, cursor.shm, cursor.schema_cookie, cursor.schema_level, cursor.epoch)
            != (fresh.dev, fresh.ino, fresh.shm, fresh.schema_cookie, fresh.schema_level, fresh.epoch)  # (c)
            or cursor.lineage != fresh.lineage):  # a segment was superseded or restored
        return fresh, None
    sql = f"SELECT session_id FROM {CHANGE_TABLE} WHERE rev > ? AND rev <= ?"
    params: tuple = (cursor.revision, fresh.revision)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit + 1,)
    return fresh, frozenset(r[0] for r in conn.execute(sql, params))
