"""Content revision of the rows the session-list kanban annotation reads, not of the file. [daveey]

The annotation's caches (``kanban_session_activity``: count fingerprint, per-board read memo, remote-links
fingerprint) were keyed on each board file's stat. A live board file is rewritten many times a minute by bookkeeping
the annotation never reads (``kanban_sync_state`` stamps, ``kanban_dispatch_stamp``, wake-check probes, a replica's
``pulled_at``), so on David-MBP the stat key moved ~61 times a minute with no task work and every sidebar poll
re-read the boards and re-projected the remote links (t_01a19b0b602137c1).

Each writer (``kanban_db_connect`` init) installs ``kanban_activity_revision``: a one-row counter bumped by triggers on
every insert, delete, or update of a read column of the tables the annotation reads. Triggers run inside the writer's
own transaction, so a committed task write is visible in the counter to the next reader (read-your-writes). Updates
that only touch bookkeeping columns (``BOOKKEEPING_COLUMNS``: claim/lease/heartbeat/sync clocks, notifier in-flight
marks) do not bump it; no activity reader selects those columns.

Readers (``content_key``) only trust the counter when every table they read that exists in the file carries all three
of its triggers; otherwise (older writer, dropped trigger, rebuilt file) they fall back to the file's stat plus a
connection-scoped SQLite data_version. Equal timestamps alone never prove an absence of commits. The key also
carries the file identity and ``schema_version``, so a replaced file or a schema
change always reads afresh.

The update trigger names each read column, so SQLite refuses ``ALTER TABLE ... DROP COLUMN`` of a watched column
while it is installed. Call ``drop_triggers(conn)`` first; the next writer init reinstalls them for the new columns
(readers fall back to the stat key in between).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterable

REVISION_TABLE = "kanban_activity_revision"
PREFIX = "kanban_activity_rev_"
# What the local-board readers read: counts/parked/stalled/unseen (_read_board), linked-card rows and their
# details (_read_session_tasks, _add_card_details), wait info. Not task_wake_checks (rewritten by every probe).
LOCAL_TABLES = ("tasks", "task_links", "task_runs", "task_events", "task_comments", "kanban_notify_subs",
                "task_chat_links")
# A sync replica (``sync-shadow/<board>.db``) is read only for this gateway's chat links and their card rows
# (``kanban_chat_links._read_replica``). Its events/runs mirror the whole board's traffic and are never read.
SHADOW_TABLES = ("tasks", "task_chat_links")
# Columns no activity reader selects, written by claims, leases, heartbeats, the sync apply path and the notifier's
# delivery claim. An update that changes only these is bookkeeping.
BOOKKEEPING_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({"claim_lock", "claim_expires", "last_heartbeat_at", "worker_pid", "worker_started_at",
                        "lease_holder", "lease_expires", "lease_version", "sync_version", "field_hlc"}),
    "task_runs": frozenset({"claim_lock", "claim_expires", "last_heartbeat_at", "worker_pid", "worker_started_at",
                            "sync_version"}),
    "kanban_notify_subs": frozenset({"inflight_from", "inflight_pid", "inflight_start", "inflight_at",
                                     "last_ping_event_id"}),
}
_BUMP = f"UPDATE {REVISION_TABLE} SET revision = revision + 1 WHERE id = 1;"


def is_shadow(path: str | os.PathLike) -> bool:
    from hermes_cli.kanban_db_sync import SHADOW_DIR
    return Path(path).parent.name == SHADOW_DIR


def tables_for(path: str | os.PathLike) -> tuple[str, ...]:
    return SHADOW_TABLES if is_shadow(path) else LOCAL_TABLES


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def wanted_triggers(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, str]:
    """Trigger name -> CREATE statement for the ``tables`` present in ``conn``, from their live columns."""
    out: dict[str, str] = {}
    for table in tables:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
        if not columns:
            continue  # table absent: nothing to read, nothing to watch
        for op in ("INSERT", "DELETE"):
            name = f"{PREFIX}{table}_{op.lower()}"
            out[name] = f"CREATE TRIGGER {name} AFTER {op} ON {_quote(table)} BEGIN {_BUMP} END"
        read = [c for c in columns if c not in BOOKKEEPING_COLUMNS.get(table, frozenset())]
        changed = " OR ".join(f"OLD.{_quote(c)} IS NOT NEW.{_quote(c)}" for c in read)
        name = f"{PREFIX}{table}_update"
        out[name] = f"CREATE TRIGGER {name} AFTER UPDATE ON {_quote(table)} WHEN {changed} BEGIN {_BUMP} END"
    return out


def ensure_revision(conn: sqlite3.Connection, tables: Iterable[str]) -> None:
    """Install or repair the counter and its triggers. No write on the normal (already installed) path; a
    (re)install bumps the counter so readers that cached a key before the gap re-read."""
    tables = tuple(tables)
    wanted = wanted_triggers(conn, tables)
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                              (PREFIX + "*",)))
    has_row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (REVISION_TABLE,)).fetchone() and conn.execute(
        f"SELECT 1 FROM {REVISION_TABLE} WHERE id = 1").fetchone()
    if has_row and found == wanted:
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {REVISION_TABLE} "
                     "(id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL)")
        conn.execute(f"INSERT OR IGNORE INTO {REVISION_TABLE} VALUES (1, 0)")
        conn.execute(_BUMP)
        for name in set(found) - set(wanted):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for name, sql in wanted.items():
            if found.get(name) != sql:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(sql)


def drop_triggers(conn: sqlite3.Connection) -> None:
    """Remove the activity triggers (not the counter), e.g. before a ``DROP COLUMN`` of a watched column. Readers
    fall back to the file's stat until the next writer init reinstalls them."""
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                                        (PREFIX + "*",))]
    for name in names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


_READ_SQL = (f"SELECT r.revision, (SELECT schema_version FROM pragma_schema_version) FROM {REVISION_TABLE} r "
             "WHERE r.id = 1")


def read_key(conn: sqlite3.Connection, tables: Iterable[str]) -> tuple | None:
    """``(revision, schema_version)`` when the counter covers every one of ``tables`` present in ``conn``, else None
    (the caller falls back to the file's stat). Raises ``sqlite3.Error`` like any read."""
    tables = tuple(tables)
    conn.execute("BEGIN")  # one snapshot: the coverage check must describe the counter it vouches for
    try:
        watched = (*tables, REVISION_TABLE)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE (type = 'trigger' AND name GLOB ?) "
            f"OR (type = 'table' AND name IN ({','.join('?' * len(watched))}))", (PREFIX + "*", *watched))}
        row = conn.execute(_READ_SQL).fetchone() if REVISION_TABLE in names else None
    finally:
        conn.execute("COMMIT")
    if row is None:
        return None
    for table in tables:
        if table in names and not all(f"{PREFIX}{table}_{op}" in names for op in ("insert", "update", "delete")):
            return None
    return (int(row[0]), int(row[1]))


def file_stat(path: Path) -> tuple:
    out = []
    for p in (path, Path(str(path) + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append(None)
    return tuple(out)


# [daveey] ``(path, device, inode, tables) -> schema_version`` at which ``read_key`` found the counter covering
# ``tables``. Every Desktop poll keys each board; the ``sqlite_master`` scan was most of that cost on David-MBP
# (t_01a19d0bcab45558). Any trigger or table change bumps ``schema_version`` (and a replaced file has a new inode),
# so a matching version proves the same coverage; one statement reads both, in one snapshot.
_COVERED: dict[tuple, int] = {}
_COVERED_LOCK = threading.Lock()
_COVERED_MAX = 256


def _covered_read(conn: sqlite3.Connection, path: Path, db: tuple | None, tables: tuple[str, ...]) -> tuple | None:
    memo_key = (str(path), db[0], db[1], tables) if db is not None else None
    with _COVERED_LOCK:
        version = _COVERED.get(memo_key) if memo_key is not None else None
    if version is not None:
        try:
            row = conn.execute(_READ_SQL).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None and int(row[1]) == version:
            return (int(row[0]), int(row[1]))
    revision = read_key(conn, tables)
    if memo_key is not None:
        with _COVERED_LOCK:
            if revision is None:
                _COVERED.pop(memo_key, None)
            else:
                if len(_COVERED) >= _COVERED_MAX:
                    _COVERED.clear()
                _COVERED[memo_key] = revision[1]
    return revision


# Zero-budget SQLite probes must not race each other's reader lifecycle opens.
# The reader cache is shared, not thread-local: serialize probes before entering
# it, so concurrent healthy readers compare the SAME connection's data_version
# rather than turning local lock contention into unrelated unknown keys. Stripes
# bound lock storage without retaining paths/connections indefinitely.
_PROBE_LOCKS = tuple(threading.RLock() for _ in range(64))


def probe_lock(path: Path):
    return _PROBE_LOCKS[hash(str(path)) % len(_PROBE_LOCKS)]


def content_key(path: Path, tables: Iterable[str], open_ro: Callable) -> tuple:
    with probe_lock(path):
        return _content_key(path, tables, open_ro)


def _content_key(path: Path, tables: Iterable[str], open_ro: Callable) -> tuple:
    """A key that changes whenever any row of ``tables`` that the readers read changes in ``path`` (and on file
    replacement or schema change), but not on bookkeeping-only writes. ``open_ro(path)`` is a context manager
    yielding a read-only connection (a kept one: the read is one indexed row plus the schema table). Falls back to
    the file's stat when the counter can't vouch for ``tables``. Never gated on the stat: a commit inside one
    mtime tick that leaves the WAL size unchanged would otherwise hide a task write from the next read."""
    tables = tuple(tables)
    stat = file_stat(path)
    db = stat[0]
    try:
        with open_ro(path) as conn:
            # data_version is meaningful only on the SAME connection. Retain its identity
            # too, so an evicted/reopened reader cannot reuse an old version's cache entry.
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            revision = _covered_read(conn, path, db, tables)
            fallback = ("stat", stat, conn, version)
    except sqlite3.Error:
        revision = None
        fallback = ("stat", stat, object())  # unreadable is unknown, never proof of unchanged content
    if revision is None or db is None:
        return fallback
    return ("revision", db[0], db[1], *revision)


def is_revision(key: tuple) -> bool:
    return bool(key) and key[0] == "revision"
