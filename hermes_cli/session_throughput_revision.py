"""Writer-side change journal for incremental machine-throughput refresh.

Design: throughput-design-v3 (kanban t_01a1d7c726355396).  Tables/triggers are installed at the very
end of SessionDB schema init; every write that can change a throughput contribution stamps a
monotonic revision, a bounded ring of (rev, nonce) anchors and a coalesced per-session journal.

Public API (reader contract):
    install(conn) -> bool
    read_stamp(conn) -> (schema_version, epoch, rev, nonce, floor) | None
    anchor_matches(conn, rev, nonce) -> bool
    changes_since(conn, rev) -> list[str]
    drop_triggers(conn)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RING_SIZE = 65536
KEY_CAP = 262144
REV_LIMIT = (1 << 63) - (1 << 20)

_TABLE_SQL = {
    "throughput_revision": (
        "CREATE TABLE throughput_revision (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL, epoch BLOB NOT NULL, rev INTEGER NOT NULL, "
        "nonce BLOB NOT NULL, floor INTEGER NOT NULL, keys INTEGER NOT NULL)"
    ),
    "throughput_anchors": (
        "CREATE TABLE throughput_anchors (slot INTEGER PRIMARY KEY, rev INTEGER NOT NULL, "
        "nonce BLOB NOT NULL)"
    ),
    "throughput_changes": (
        "CREATE TABLE throughput_changes (kind TEXT NOT NULL, session_id TEXT NOT NULL, "
        "rev INTEGER NOT NULL, PRIMARY KEY (kind, session_id))"
    ),
}
_INDEX_NAME = "throughput_changes_rev"
_INDEX_SQL = "CREATE INDEX throughput_changes_rev ON throughput_changes(rev)"

_ROTATE = "epoch = randomblob(16), floor = rev, keys = 0"


def _stamp(kind: str, x: str, extra: str = "1") -> str:
    """SQL statements stamping session key expression ``x`` (a NEW./OLD. reference)."""
    text = f"typeof({x}) = 'text' AND ({extra})"
    nontext = f"typeof({x}) IS NOT 'text' AND ({extra})"
    absent = f"NOT EXISTS (SELECT 1 FROM throughput_changes WHERE kind = '{kind}' AND session_id = {x})"
    return "\n".join([
        # unsupported (NULL/non-text) key: invalidate every cursor, journal nothing
        f"UPDATE throughput_revision SET {_ROTATE} WHERE {nontext};",
        # key-cap rotation BEFORE the incoming key is counted
        f"UPDATE throughput_revision SET {_ROTATE} WHERE {text} AND keys >= {KEY_CAP} AND {absent};",
        # Indexed empty range on ordinary writes; do not scan the journal just
        # to discover that no rotation happened. Clear before revision reset.
        f"DELETE FROM throughput_changes WHERE rev <= (SELECT CASE WHEN keys = 0 "
        f"OR (rev >= {REV_LIMIT} AND {text}) THEN rev ELSE -1 END FROM throughput_revision);",
        # revision exhaustion: exceptional full reset
        f"UPDATE throughput_revision SET epoch = randomblob(16), rev = 0, floor = 0, keys = 0 "
        f"WHERE {text} AND rev >= {REV_LIMIT};",
        f"UPDATE throughput_revision SET rev = rev + 1, nonce = randomblob(16), keys = keys + ({absent}) "
        f"WHERE {text};",
        f"INSERT INTO throughput_anchors(slot, rev, nonce) SELECT rev % {RING_SIZE}, rev, nonce "
        f"FROM throughput_revision WHERE {text} "
        "ON CONFLICT(slot) DO UPDATE SET rev = excluded.rev, nonce = excluded.nonce;",
        f"INSERT INTO throughput_changes(kind, session_id, rev) SELECT '{kind}', {x}, rev "
        f"FROM throughput_revision WHERE {text} "
        "ON CONFLICT(kind, session_id) DO UPDATE SET rev = excluded.rev;",
    ])


_ROTATE_STMT = f"UPDATE throughput_revision SET {_ROTATE};\nDELETE FROM throughput_changes;"


def _trigger(name: str, event: str, table: str, when: str, body: str) -> str:
    when_sql = f"\nWHEN {when}" if when else ""
    return f"CREATE TRIGGER {name}\n{event} ON {table}{when_sql}\nBEGIN\n{body}\nEND"


def _build_triggers() -> dict[str, str]:
    s_insert_guard = (
        "(EXISTS (SELECT 1 FROM sessions WHERE rowid = NEW.rowid AND id IS NOT NEW.id) "
        "OR (NEW.title IS NOT NULL AND EXISTS (SELECT 1 FROM sessions "
        "WHERE title = NEW.title AND id IS NOT NEW.id)))"
    )
    s_update_guard = (
        "(NEW.id IS NOT OLD.id OR NEW.title IS NOT OLD.title OR NEW.rowid IS NOT OLD.rowid) AND "
        "(EXISTS (SELECT 1 FROM sessions WHERE rowid = NEW.rowid AND rowid IS NOT OLD.rowid) "
        "OR (NEW.id IS NOT NULL AND EXISTS (SELECT 1 FROM sessions "
        "WHERE id = NEW.id AND rowid IS NOT OLD.rowid)) "
        "OR (NEW.title IS NOT NULL AND EXISTS (SELECT 1 FROM sessions "
        "WHERE title = NEW.title AND rowid IS NOT OLD.rowid)))"
    )
    u_insert_guard = (
        "EXISTS (SELECT 1 FROM session_model_usage WHERE rowid = NEW.rowid "
        "AND session_id IS NOT NEW.session_id)"
    )
    u_update_guard = (
        "NEW.rowid IS NOT OLD.rowid AND EXISTS (SELECT 1 FROM session_model_usage "
        "WHERE rowid = NEW.rowid AND rowid IS NOT OLD.rowid AND session_id IS NOT NEW.session_id)"
    )
    changed_end = "OLD.id IS NOT NEW.id OR (OLD.ended_at IS NULL) IS NOT (NEW.ended_at IS NULL)"
    return {
        "throughput_sessions_bi": _trigger(
            "throughput_sessions_bi", "BEFORE INSERT", "sessions", s_insert_guard, _ROTATE_STMT),
        "throughput_sessions_bu": _trigger(
            "throughput_sessions_bu", "BEFORE UPDATE", "sessions", s_update_guard, _ROTATE_STMT),
        "throughput_sessions_ai": _trigger(
            "throughput_sessions_ai", "AFTER INSERT", "sessions", "", _stamp("s", "NEW.id")),
        "throughput_sessions_ad": _trigger(
            "throughput_sessions_ad", "AFTER DELETE", "sessions", "", _stamp("s", "OLD.id")),
        "throughput_sessions_au": _trigger(
            "throughput_sessions_au", "AFTER UPDATE OF id, ended_at", "sessions", changed_end,
            _stamp("s", "OLD.id") + "\n" + _stamp("s", "NEW.id", "NEW.id IS NOT OLD.id")),
        "throughput_usage_bi": _trigger(
            "throughput_usage_bi", "BEFORE INSERT", "session_model_usage", u_insert_guard, _ROTATE_STMT),
        "throughput_usage_bu": _trigger(
            "throughput_usage_bu", "BEFORE UPDATE", "session_model_usage", u_update_guard, _ROTATE_STMT),
        "throughput_usage_ai": _trigger(
            "throughput_usage_ai", "AFTER INSERT", "session_model_usage", "",
            _stamp("u", "NEW.session_id")),
        "throughput_usage_ad": _trigger(
            "throughput_usage_ad", "AFTER DELETE", "session_model_usage", "",
            _stamp("u", "OLD.session_id")),
        "throughput_usage_au": _trigger(
            "throughput_usage_au", "AFTER UPDATE", "session_model_usage", "",
            _stamp("u", "OLD.session_id")
            + "\n" + _stamp("u", "NEW.session_id", "NEW.session_id IS NOT OLD.session_id")),
    }


_TRIGGERS = _build_triggers()


def drop_triggers(conn: sqlite3.Connection) -> None:
    """Drop every throughput tracking trigger (for migrations that rebuild tracked tables)."""
    for name in _TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


# --------------------------------------------------------------------------- shape checks

def _norm(sql: Optional[str]) -> str:
    return re.sub(r"\s+", " ", sql or "").strip().lower()


def _master(conn, type_: str) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = ?", (type_,))}


def _columns(conn, table: str) -> dict[str, tuple]:
    # name -> (declared type upper, notnull, pk)
    return {r[1]: (str(r[2]).upper(), r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _unique_indexes(conn, table: str) -> Optional[list[tuple]]:
    """[(name, origin, partial, [(column, collation)...])] for unique indexes; None if expression key."""
    out = []
    for _seq, name, unique, origin, partial in conn.execute(f"PRAGMA index_list({table})"):
        if not unique:
            continue
        cols = []
        for _s, cid, cname, _desc, coll, key in conn.execute(f"PRAGMA index_xinfo({name!r})"):
            if not key:
                continue
            if cid < 0 or cname is None:
                return None
            cols.append((cname, str(coll).upper()))
        out.append((name, origin, bool(partial), cols))
    return out


def _supported_shape(conn: sqlite3.Connection) -> bool:
    try:
        tables = _master(conn, "table")
        for needed in ("sessions", "session_model_usage", "session_admissions", "worker_executions"):
            if needed not in tables:
                return False
        for t in ("sessions", "session_model_usage"):
            # Index collation can override a column's declared collation. The
            # canonical tables declare none; unfamiliar declarations use SQL.
            if re.search(r"\bcollate\b|without\s+rowid", tables[t] or "", re.IGNORECASE):
                return False
            if {n.lower() for n in _columns(conn, t)} & {"rowid", "_rowid_", "oid"}:
                return False
        sc = _columns(conn, "sessions")
        if sc.get("id", ("", 0, 0))[0] != "TEXT" or sc["id"][2] != 1:
            return False
        if "ended_at" not in sc or "title" not in sc:
            return False
        if sum(1 for v in sc.values() if v[2]) != 1:
            return False
        idx_sql = _master(conn, "index")
        uniq = _unique_indexes(conn, "sessions")
        if uniq is None or not any(row[0] == "idx_sessions_title_unique" for row in uniq):
            return False
        for name, origin, partial, cols in uniq:
            if origin == "pk" and not partial and cols == [("id", "BINARY")]:
                continue
            if (name == "idx_sessions_title_unique" and origin == "c" and partial
                    and cols == [("title", "BINARY")]
                    and _norm(idx_sql.get(name)).partition(" where ")[2] == "title is not null"):
                continue
            return False
        uc = _columns(conn, "session_model_usage")
        for col, typ in (("session_id", "TEXT"), ("model", "TEXT"), ("input_tokens", "INTEGER"),
                         ("output_tokens", "INTEGER"), ("cache_read_tokens", "INTEGER"),
                         ("cache_write_tokens", "INTEGER")):
            if uc.get(col, ("", 0, 0))[0] != typ:
                return False
        if not (uc["session_id"][1] and uc["model"][1]):
            return False
        pk = ["session_id", "model", "billing_provider", "billing_base_url", "billing_mode", "task"]
        uuniq = _unique_indexes(conn, "session_model_usage")
        if uuniq is None or len(uuniq) != 1:
            return False
        name, origin, partial, cols = uuniq[0]
        return origin == "pk" and not partial and cols == [(c, "BINARY") for c in pk]
    except sqlite3.Error:
        return False


def _tables_exact(conn) -> Optional[bool]:
    """True exact, False absent entirely, None present-but-different."""
    tables = _master(conn, "table")
    present = [n for n in _TABLE_SQL if n in tables]
    if not present:
        return False
    if len(present) != len(_TABLE_SQL):
        return None
    if any(tables[n] != _TABLE_SQL[n] for n in _TABLE_SQL):
        return None
    if _master(conn, "index").get(_INDEX_NAME) != _INDEX_SQL:
        return None
    return True


def _triggers_exact(conn) -> bool:
    live = _master(conn, "trigger")
    return all(live.get(n) == sql for n, sql in _TRIGGERS.items())


def _invariant_ok(conn) -> bool:
    row = conn.execute(
        "SELECT version, rev, floor, keys, epoch, nonce FROM throughput_revision WHERE id = 1").fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        return False
    _v, rev, floor, keys, epoch, nonce = row
    if any(type(n) is not int for n in (_v, rev, floor, keys)):
        return False
    if any(not isinstance(n, bytes) or len(n) != 16 for n in (epoch, nonce)):
        return False
    if not (0 <= floor <= rev <= (1 << 63) - 1 and 0 <= keys <= KEY_CAP):
        return False
    mx = conn.execute("SELECT MAX(rev) FROM throughput_changes").fetchone()[0]
    if mx is None:
        return rev == floor and keys == 0
    return mx == rev


# --------------------------------------------------------------------------- install

def settle_schema(conn: sqlite3.Connection) -> bool:
    """Settled includes intentionally unsupported shapes; failures remain retryable."""
    if install(conn):
        return True
    try:
        if not _supported_shape(conn):
            return True
        if _tables_exact(conn) is None:
            from hermes_cli.session_throughput_repair import disable_stranded

            disabled = disable_stranded(conn)
            return True if disabled is None else disabled
        if _tables_exact(conn) is True:
            row = conn.execute("SELECT version FROM throughput_revision WHERE id=1").fetchone()
            if row is not None and type(row[0]) is int and row[0] > SCHEMA_VERSION:
                return True
    except sqlite3.Error:
        pass
    return False


def install(conn: sqlite3.Connection) -> bool:
    """Idempotently install/repair tracking. Atomic; returns True iff tracking is usable."""
    try:
        conn.execute("SAVEPOINT throughput_install")
    except sqlite3.Error:
        return False
    ok = False
    try:
        ok = _install_inner(conn)
    except sqlite3.Error as exc:
        logger.debug("throughput tracking install failed: %s", exc)
        ok = False
    try:
        if ok:
            conn.execute("RELEASE throughput_install")
        else:
            conn.execute("ROLLBACK TO throughput_install")
            conn.execute("RELEASE throughput_install")
    except sqlite3.Error:
        return False
    return ok


def _install_inner(conn: sqlite3.Connection) -> bool:
    if not _supported_shape(conn):
        return False
    state = _tables_exact(conn)
    if state is None:
        return False  # foreign / future definition: leave untouched
    if state is False:
        for sql in _TABLE_SQL.values():
            conn.execute(sql)
        conn.execute(_INDEX_SQL)
        conn.execute(
            "INSERT INTO throughput_revision(id, version, epoch, rev, nonce, floor, keys) "
            "VALUES (1, ?, randomblob(16), 0, randomblob(16), 0, 0)", (SCHEMA_VERSION,))
        conn.execute(
            "INSERT INTO throughput_anchors(slot, rev, nonce) SELECT 0, rev, nonce "
            "FROM throughput_revision")
    else:
        row = conn.execute("SELECT version FROM throughput_revision WHERE id = 1").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            return False  # future (or corrupt) version: never rewritten
    if not _triggers_exact(conn):
        drop_triggers(conn)
        for sql in _TRIGGERS.values():
            conn.execute(sql)
        if state is not False:
            # writes in the gap were invisible: invalidate every cursor
            conn.execute(f"UPDATE throughput_revision SET {_ROTATE}")
            conn.execute("DELETE FROM throughput_changes")
    current = conn.execute("SELECT rev, nonce FROM throughput_revision WHERE id=1").fetchone()
    if not _invariant_ok(conn) or current is None or not anchor_matches(conn, *current):
        conn.execute(f"UPDATE throughput_revision SET {_ROTATE}")
        conn.execute("DELETE FROM throughput_changes")
        # rev may be < floor-consistent only if corrupt; anchor must exist for the current rev
        conn.execute(
            f"INSERT INTO throughput_anchors(slot, rev, nonce) SELECT rev % {RING_SIZE}, rev, nonce "
            "FROM throughput_revision WHERE true "
            "ON CONFLICT(slot) DO UPDATE SET rev = excluded.rev, nonce = excluded.nonce")
        if not _invariant_ok(conn):
            return False
    return True


# --------------------------------------------------------------------------- reader API

def read_stamp(conn: sqlite3.Connection) -> Optional[tuple]:
    """(schema_version, epoch, rev, nonce, floor) when tracking is fully valid, else None."""
    try:
        if not _supported_shape(conn) or _tables_exact(conn) is not True or not _triggers_exact(conn):
            return None
        if not _invariant_ok(conn):
            return None
        row = conn.execute(
            "SELECT version, epoch, rev, nonce, floor FROM throughput_revision WHERE id = 1").fetchone()
        if row is None or not anchor_matches(conn, row[2], row[3]):
            return None
        schema = conn.execute("PRAGMA schema_version").fetchone()[0]
        return (schema, *tuple(row)[1:])
    except sqlite3.Error:
        return None


def anchor_matches(conn: sqlite3.Connection, rev: int, nonce: bytes) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM throughput_anchors WHERE slot = ? AND rev = ? AND nonce = ?",
            (rev % RING_SIZE, rev, nonce)).fetchone()
    except (sqlite3.Error, TypeError):
        return False
    return row is not None


def changes_since(conn: sqlite3.Connection, rev: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM throughput_changes WHERE rev > ?", (rev,))]
