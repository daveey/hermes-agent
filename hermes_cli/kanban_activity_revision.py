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
marks) do not bump it; no activity reader selects those columns. Neither do inserts of ``UNREAD_EVENT_KINDS``
events (heartbeats), whose only reader (the card-details strip) is never cached on this key. A writer of an older
version reinstalls its unconditional insert trigger on init (``ensure_revision`` repairs any SQL mismatch, bumping):
heartbeats then bump again, a superset, and readers of either version only check the trigger names.

``projection_revision`` is the key the links and annotation caches compare. It bumps with ``revision`` except on
``task_events`` inserts whose kind is not in ``PROJECTION_EVENT_KINDS`` (the kinds a cached activity reader selects).
A comment, claim or worker-lifecycle event still moves ``revision``, so the notify gate and the host change feed
still wake; it does not move the projection key, so those caches do not re-project thousands of rows for an event
they do not read. A file whose triggers predate the column falls back to ``revision`` (never a hidden change).

Readers (``content_key``) only trust the counter when every table they read that exists in the file carries all three
of its triggers; otherwise (older writer, dropped trigger, rebuilt file) they fall back to the file's stat plus a
connection-scoped SQLite data_version. Equal timestamps alone never prove an absence of commits. The key also
carries the file identity and ``schema_version``, so a replaced file or a schema
change always reads afresh.

The update trigger names each read column, so SQLite refuses ``ALTER TABLE ... DROP COLUMN`` of a watched column
while it is installed. Call ``drop_triggers(conn)`` first; the next writer init reinstalls them for the new columns
(readers fall back to the stat key in between).

Per-task change cursor (P2, t_01a1d59d3ef805c4). Every bumping trigger also upserts ``kanban_task_change(task_id,
rev)`` with the bumped revision, for each task the row belongs to (a task's ``id``; both ends of a link; the
``task_id`` of every other table; OLD and NEW when an update moves it; OLD on delete). ``kanban_task_change`` then says
which tasks moved after any earlier revision (``kanban_task_change.changed_since``). A bump no task explains (a
trigger (re)install) upserts the ``FULL_MARK`` row, which tells a cursor older than it to read everything. Triggers of
this design carry ``EPOCH_MARK`` in their SQL; ``ensure_revision`` never drops or rewrites triggers a newer epoch
installed, so a newer writer's coverage survives this one's init (an older writer still replaces ours: the cursor's
trust rules catch that).
"""

from __future__ import annotations

import os
import re
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
# A sync replica (``sync-shadow/<board>.db``) is read for this gateway's chat links and their card rows
# (``kanban_chat_links._read_replica``), and by the sync loop's parent evidence (``kanban_move_parent_evidence``: the
# replica's ``task_runs``/``task_events``) and remote-parent refresh. [daveey] P2/R8 (t_01a1d59d3ef805c4): it watches
# the same task tables as a local board, so the per-task change cursor covers runs, events, links and comments too,
# with the same exclusions (bookkeeping columns, heartbeat events): a pulled heartbeat still does not move it.
SHADOW_TABLES = ("tasks", "task_links", "task_runs", "task_events", "task_comments", "task_chat_links")
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
# [daveey] Event kinds no keyed reader reads: a heartbeat is a liveness signal (``heartbeat_worker``: ~18 of the hub
# board's ~39 bumps a minute) that only the card-details strip shows, and that read is never cached on this key
# (``_add_card_details`` runs on every ``session_tasks`` call). Their inserts do not bump the counter, so a heartbeat
# no longer re-reads every board's counts, the links projection and the attention pass (t_01a1cee98aaa8db1). Every
# other kind, and any update or delete of an event row, still bumps.
UNREAD_EVENT_KINDS = ("heartbeat",)
# Kinds a cached activity reader selects from task_events (session-activity UNSEEN_KINDS). An insert of any
# other kind still bumps ``revision`` (notify reads it) but not ``projection_revision``.
PROJECTION_EVENT_KINDS = ("blocked", "completed", "crashed", "gave_up", "status", "timed_out")
_BUMP = f"UPDATE {REVISION_TABLE} SET revision = revision + 1 WHERE id = 1;"
_BUMP_PROJECTION = (f"UPDATE {REVISION_TABLE} SET projection_revision = projection_revision + 1 WHERE id = 1;")
# [daveey] P2 per-task change cursor (t_01a1d59d3ef805c4): which tasks each bump touched.
CHANGE_TABLE = "kanban_task_change"
FULL_MARK = ""  # a bump no task explains (trigger (re)install): cursors older than its rev read in full
EPOCH = 3  # 1 = bump-only; 2 = bump + kanban_task_change; 3 = projection_revision beside revision
EPOCH_MARK = f"/* kanban-activity-epoch:{EPOCH} */"
_EPOCH_RE = re.compile(r"/\* kanban-activity-epoch:(\d+) \*/")
# The task(s) a row belongs to. Every other watched table names it in ``task_id``.
TASK_KEYS: dict[str, tuple[str, ...]] = {"tasks": ("id",), "task_links": ("parent_id", "child_id")}
CHANGE_INDEX = f"idx_{CHANGE_TABLE}_rev"
# A rowid table, not WITHOUT ROWID: generic every-table readers (``SELECT * ... ORDER BY rowid`` snapshots, recovery
# scans) expect a rowid on every board table, and the cost difference on a one-row-per-task table is noise.
_CHANGE_DDL = (f"CREATE TABLE IF NOT EXISTS {CHANGE_TABLE} (task_id TEXT PRIMARY KEY NOT NULL, "
               "rev INTEGER NOT NULL)",
               f"CREATE INDEX IF NOT EXISTS {CHANGE_INDEX} ON {CHANGE_TABLE}(rev)")


def _mark(ref: str, when: str = "") -> str:
    """Upsert the task ``ref`` (``NEW.x``/``OLD.x``, NULL = ``FULL_MARK``) at the revision just bumped.

    An ``ON CONFLICT DO UPDATE`` upsert, never ``INSERT OR REPLACE``: an outer statement's conflict policy overrides a
    trigger body's ``OR`` policy, so ``INSERT OR IGNORE INTO task_links`` silently dropped the mark (while the bump
    stayed) and ``INSERT OR ABORT`` raised UNIQUE on this table. No outer policy overrides an upsert clause. (The
    ``WHERE`` is required: it keeps SQLite from parsing ``ON CONFLICT`` as a join constraint.)"""
    return (f"INSERT INTO {CHANGE_TABLE} (task_id, rev) SELECT COALESCE({ref}, '{FULL_MARK}'), revision "
            f"FROM {REVISION_TABLE} WHERE id = 1{when} ON CONFLICT(task_id) DO UPDATE SET rev = excluded.rev;")


def _marks(op: str, keys: tuple[str, ...]) -> str:
    if not keys:  # an unexpected schema with no task column: the bump is unattributable
        return _mark("NULL")
    if op == "INSERT":
        return " ".join(_mark(f"NEW.{_quote(k)}") for k in keys)
    if op == "DELETE":
        return " ".join(_mark(f"OLD.{_quote(k)}") for k in keys)
    return " ".join(_mark(f"NEW.{_quote(k)}") + " " + _mark(
        f"OLD.{_quote(k)}", f" AND OLD.{_quote(k)} IS NOT NEW.{_quote(k)}") for k in keys)


def trigger_epoch(sql: str | None) -> int:
    """The design epoch a trigger's SQL was generated by (1 for the unmarked bump-only triggers)."""
    found = _EPOCH_RE.search(sql or "")
    return int(found.group(1)) if found else 1


# [daveey] Columns the sync insert trigger stamps onto the row it just inserted (``kanban_db_sync._insert_trigger``:
# ``UPDATE task_events SET uid = ... WHERE id = NEW.id AND uid IS NULL``), inside the inserting statement. On a synced
# board that update re-bumped every heartbeat the INSERT trigger had excluded, so heartbeats still moved the revision
# (and would mark their task). NULL -> value is that stamp; any later change of the column still counts.
SYNC_STAMPED: dict[str, frozenset[str]] = {"task_events": frozenset({"uid"})}


def _moved(table: str, column: str) -> str:
    q = _quote(column)
    if column in SYNC_STAMPED.get(table, frozenset()):
        return f"(OLD.{q} IS NOT NULL AND OLD.{q} IS NOT NEW.{q})"
    return f"OLD.{q} IS NOT NEW.{q}"


def is_shadow(path: str | os.PathLike) -> bool:
    from hermes_cli.kanban_db_sync import SHADOW_DIR
    return Path(path).parent.name == SHADOW_DIR


def tables_for(path: str | os.PathLike) -> tuple[str, ...]:
    return SHADOW_TABLES if is_shadow(path) else LOCAL_TABLES


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _projection_bump(table: str, op: str, columns: list[str]) -> str:
    """Bump ``projection_revision``. Event inserts bump it only for kinds a cached reader selects."""
    if op == "INSERT" and table == "task_events" and "kind" in columns:
        kinds = ", ".join(f"'{k}'" for k in PROJECTION_EVENT_KINDS)
        return (f"UPDATE {REVISION_TABLE} SET projection_revision = projection_revision + 1 WHERE id = 1 "
                f"AND COALESCE(NEW.kind, '') IN ({kinds});")
    return _BUMP_PROJECTION


def wanted_triggers(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, str]:
    """Trigger name -> CREATE statement for the ``tables`` present in ``conn``, from their live columns."""
    out: dict[str, str] = {}
    for table in tables:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
        if not columns:
            continue  # table absent: nothing to read, nothing to watch
        keys = tuple(k for k in TASK_KEYS.get(table, ("task_id",)) if k in columns)
        for op in ("INSERT", "DELETE"):
            name = f"{PREFIX}{table}_{op.lower()}"
            when = ""
            if op == "INSERT" and table == "task_events" and "kind" in columns:
                when = " WHEN COALESCE(NEW.kind, '') NOT IN ({})".format(", ".join(f"'{k}'" for k in UNREAD_EVENT_KINDS))
            out[name] = (f"CREATE TRIGGER {name} AFTER {op} ON {_quote(table)}{when} BEGIN {EPOCH_MARK} {_BUMP} "
                         f"{_projection_bump(table, op, columns)} {_marks(op, keys)} END")
        read = [c for c in columns if c not in BOOKKEEPING_COLUMNS.get(table, frozenset())]
        changed = " OR ".join(_moved(table, c) for c in read)
        name = f"{PREFIX}{table}_update"
        out[name] = (f"CREATE TRIGGER {name} AFTER UPDATE ON {_quote(table)} WHEN {changed} BEGIN {EPOCH_MARK} "
                     f"{_BUMP} {_projection_bump(table, 'UPDATE', columns)} {_marks('UPDATE', keys)} END")
    return out


def ensure_revision(conn: sqlite3.Connection, tables: Iterable[str]) -> None:
    """Install or repair the counter, the change table and the triggers. No write on the normal (already installed)
    path; a (re)install bumps the counter and marks ``FULL_MARK`` at it, so readers that cached a key or a cursor
    before the gap re-read. Triggers of a newer epoch (``trigger_epoch``) are left alone, with every other trigger of
    the set: the newer writer that installed them owns the file's coverage."""
    tables = tuple(tables)
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
                              (PREFIX + "*",)))
    if any(trigger_epoch(sql) > EPOCH for sql in found.values()):
        return
    wanted = wanted_triggers(conn, tables)
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN (?, ?, ?)",
                                          (REVISION_TABLE, CHANGE_TABLE, CHANGE_INDEX))}
    has_row = present == {REVISION_TABLE, CHANGE_TABLE, CHANGE_INDEX} and conn.execute(
        f"SELECT 1 FROM {REVISION_TABLE} WHERE id = 1").fetchone()
    has_projection = False
    if REVISION_TABLE in present:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(REVISION_TABLE)})")}
        has_projection = "projection_revision" in cols
    if has_row and has_projection and found == wanted:
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {REVISION_TABLE} "
                     "(id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL)")
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(REVISION_TABLE)})")}
        if "projection_revision" not in cols:
            conn.execute(f"ALTER TABLE {REVISION_TABLE} ADD COLUMN projection_revision INTEGER NOT NULL DEFAULT 0")
        conn.execute(f"INSERT OR IGNORE INTO {REVISION_TABLE} (id, revision) VALUES (1, 0)")
        for ddl in _CHANGE_DDL:
            conn.execute(ddl)
        conn.execute(_BUMP)
        conn.execute(_BUMP_PROJECTION)
        conn.execute(_mark("NULL"))  # whatever changed while the triggers were absent or older is unknown
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
    lock = probe_lock(path)
    if not lock.acquire(timeout=0.05):
        # Briefly serialize healthy probes so legacy data_version keys share one
        # observer; never convoy indefinitely behind a slow reader. Unknown keys
        # are deliberately unequal, so they cannot certify a cached answer.
        return ("stat", file_stat(path), object())
    try:
        return _content_key(path, tables, open_ro)
    finally:
        lock.release()


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


def read_projection_key(conn: sqlite3.Connection, tables: Iterable[str]) -> tuple | None:
    """``(projection_revision, schema_version)`` when epoch-3 triggers cover ``tables``, else None.

    None means the caller must use ``revision`` (the notify counter). Never treat a missing column as
    "unchanged": an older file has no projection counter, and hiding its writes would drop a real change.
    """
    tables = tuple(tables)
    conn.execute("BEGIN")
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_quote(REVISION_TABLE)})")}
        if "projection_revision" not in cols:
            return None
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                                (f"{PREFIX}task_events_insert",)).fetchone()
        if trigger is None or "projection_revision" not in (trigger[0] or ""):
            return None
        watched = (*tables, REVISION_TABLE)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE (type = 'trigger' AND name GLOB ?) "
            f"OR (type = 'table' AND name IN ({','.join('?' * len(watched))}))", (PREFIX + "*", *watched))}
        row = conn.execute(
            f"SELECT projection_revision, (SELECT schema_version FROM pragma_schema_version) "
            f"FROM {REVISION_TABLE} WHERE id = 1").fetchone()
    finally:
        conn.execute("COMMIT")
    if row is None:
        return None
    for table in tables:
        if table in names and not all(f"{PREFIX}{table}_{op}" in names for op in ("insert", "update", "delete")):
            return None
    return (int(row[0]), int(row[1]))


def projection_content_key(path: Path, tables: Iterable[str], open_ro: Callable) -> tuple | None:
    """The links/annotation key, or None when this file has no projection counter (caller uses ``content_key``)."""
    lock = probe_lock(path)
    if not lock.acquire(timeout=0.05):
        return None
    try:
        db = file_stat(path)[0]
        if db is None:
            return None
        try:
            with open_ro(path) as conn:
                row = read_projection_key(conn, tables)
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return ("projection", db[0], db[1], *row)
    finally:
        lock.release()


def is_revision(key: tuple) -> bool:
    """True for a trigger-backed key (the notify counter or the links/annotation projection counter)."""
    return bool(key) and key[0] in ("revision", "projection")


def covered_revision(conn: sqlite3.Connection, path: Path, ident: tuple, tables: Iterable[str]) -> tuple | None:
    """``(revision, schema_version)`` on ``conn`` when the counter covers every one of ``tables``, else None (an
    older writer dropped the triggers: the caller reads the rows). ``ident`` is the file's (dev, ino); a matching
    ``schema_version`` reuses the coverage proof, so the warm path is one statement. Must run outside a
    transaction (a miss opens its own snapshot). Raises ``sqlite3.Error`` like any read."""
    return _covered_read(conn, path, tuple(ident), tuple(tables))
