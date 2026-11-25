"""Independent per-part revisions for the session-list kanban annotation.

Two one-row counters, each bumped only by triggers generated from the *actual* columns of the
tables its part reads (``PRAGMA table_xinfo``), so a column added later is covered by the next
writable init and, until then, makes the reader refuse (fail-fresh):

* COUNTS    -- everything the board counts/projection reads: ``tasks`` (all columns but the task's
  ``last_heartbeat_at``), ``task_runs`` (ALL columns, heartbeat included), ``task_events`` (every
  kind, every column, so a heartbeat event bumps), links, notify subscriptions, chat links, remote
  parents, ``kanban_sync_state`` (``board_id``/``gateway_id``/``mode`` only) and the containment
  evidence inputs the contained display reads. Comment rows do not bump it.
* ATTENTION -- ``tasks`` (same exclusion), ``task_runs`` (all columns), ``kanban_notify_subs`` (all).

Installation is ``ensure_revisions`` and belongs to writable board initialization only. Readers
(``read_counts_revision`` / ``read_attention_revision``) never write or repair: inside one snapshot
they compare ``sqlite_master`` byte-for-byte with the SQL regenerated from the live columns, and
return ``None`` for anything else (missing/partial/foreign counter or trigger, a required table
absent or malformed, a view posing as a table, a ``containment_*`` table this module does not
know). ``None`` means "read literally"; a revision is ``(*identity, counter, schema_version)``.

A lost counter is repaired with a bump *and* trigger re-creation, which changes the schema version,
so a restarted counter value cannot match a cached key. Restoring a file's identity is the
caller's concern.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# --- table specs -------------------------------------------------------------------------------
# mode: "all" = every column; ("except", cols); ("only", cols)
_ALL = "all"
TASK_HEARTBEAT = ("except", frozenset({"last_heartbeat_at"}))
SYNC_STATE_COLUMNS = ("only", frozenset({"board_id", "gateway_id", "mode"}))

COUNTS_TABLES: dict[str, object] = {
    "tasks": TASK_HEARTBEAT,
    "task_runs": _ALL,
    "task_events": _ALL,
    "task_links": _ALL,
    "kanban_notify_subs": _ALL,
    "task_chat_links": _ALL,
    "kanban_sync_remote_parents": _ALL,
    "kanban_sync_state": SYNC_STATE_COLUMNS,
    # Containment evidence the contained display reads (kanban_contained_display). The attestation
    # ledger is ``containment_attestations``; ``containment_local_attestations`` is accepted as the
    # alternate name should a board carry it.
    "containment_canonical_receipts": _ALL,
    "containment_sync_acceptances": _ALL,
    "containment_attestations": _ALL,
    "containment_local_attestations": _ALL,
}
ATTENTION_TABLES: dict[str, object] = {
    "tasks": TASK_HEARTBEAT,
    "task_runs": _ALL,
    "kanban_notify_subs": _ALL,
}
REQUIRED = {
    # Absent optional tables are certified absent by the schema cookie, not silently ignored.
    # Their later creation invalidates the key before any new rows can be reused.
    "counts": frozenset({"tasks"}),
    "attention": frozenset({"tasks"}),
}
# containment_* tables that exist in current writers but feed no annotation read.
KNOWN_UNREAD_CONTAINMENT = frozenset({
    "containment_observations", "containment_delivery", "containment_local_stops",
    "containment_closure_backfill_retry", "containment_sync_withheld", "containment_sync_quarantine",
    "containment_sync_quarantine_tasks", "containment_sync_delete_owner",
})


@dataclass(frozen=True)
class Kind:
    name: str
    table: str
    prefix: str
    tables: dict

    @property
    def ddl(self) -> str:
        return f"CREATE TABLE {self.table} (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"

    @property
    def bump(self) -> str:
        return f"UPDATE {self.table} SET revision = revision + 1 WHERE id = 1;"


COUNTS = Kind("counts", "kanban_annotation_counts_revision", "kanban_annotation_counts_", COUNTS_TABLES)
ATTENTION = Kind("attention", "kanban_annotation_attention_revision", "kanban_annotation_attention_",
                 ATTENTION_TABLES)
LINKS = Kind("links", "kanban_annotation_links_revision", "kanban_annotation_links_", COUNTS_TABLES)
KINDS = (COUNTS, ATTENTION, LINKS)
REQUIRED['links'] = REQUIRED['counts']


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _master(conn):
    return {name: (typ, sql) for name, typ, sql in conn.execute("SELECT name, type, sql FROM sqlite_master")}


def _columns(conn, table: str) -> list[str]:
    # hidden 0 = ordinary, 2/3 = generated; 1 = virtual-table hidden (not a stored column)
    return [r[1] for r in conn.execute(f"PRAGMA table_xinfo({_q(table)})") if r[6] in (0, 2, 3)]


def _selected(spec, columns: list[str]) -> list[str]:
    if spec == _ALL:
        return list(columns)
    mode, names = spec
    return [c for c in columns if (c not in names if mode == "except" else c in names)]


def _expected(conn, kind: Kind, master: dict):
    """Exact ``{name: sql}`` the schema must hold, or ``None`` when coverage cannot be certified.

    Containment ``containment_*`` tables outside the enumerated sets are refused here, and so is any
    required table that is missing, not a real table, or lacks a selectable column.
    """
    wanted = {kind.table: kind.ddl}
    for name, spec in kind.tables.items():
        entry = master.get(name)
        if entry is None:
            if name in REQUIRED[kind.name]:
                return None
            continue
        if entry[0] != "table":
            return None
        cols = _selected(spec, _columns(conn, name))
        if not cols or (spec != _ALL and spec[0] == "only" and set(cols) != set(spec[1])):
            return None
        for op in ("INSERT", "DELETE", "UPDATE"):
            when = ""
            if kind is LINKS and name == 'task_events' and op == 'INSERT':
                from hermes_cli.kanban_activity_revision import PROJECTION_EVENT_KINDS
                kinds = ','.join("'" + v + "'" for v in PROJECTION_EVENT_KINDS)
                when = f' WHEN NEW.kind IN ({kinds})'
            if op == "UPDATE":
                when = " WHEN " + " OR ".join(f"OLD.{_q(c)} IS NOT NEW.{_q(c)}" for c in cols)
            trig = f"{kind.prefix}{name}_{op.lower()}"
            wanted[trig] = (f"CREATE TRIGGER {trig} AFTER {op} ON {_q(name)}{when} BEGIN {kind.bump} END")
    return wanted


def _foreign_containment(master: dict) -> bool:
    known = set(COUNTS_TABLES) | KNOWN_UNREAD_CONTAINMENT
    return any(n.startswith("containment_") and t in ("table", "view") and n not in known
               for n, (t, _s) in master.items())


def _ours(master: dict, kind: Kind) -> dict:
    return {n: s for n, (t, s) in master.items()
            if (n == kind.table and t == "table") or (t == "trigger" and n.startswith(kind.prefix))}


def _intact(conn, kind: Kind, master: dict) -> bool:
    wanted = _expected(conn, kind, master)
    if wanted is None or _ours(master, kind) != wanted:
        return False
    return conn.execute(f"SELECT 1 FROM {kind.table} WHERE id = 1").fetchone() is not None


def ensure_revisions(conn: sqlite3.Connection) -> None:
    """Writable-init installer. Intact schemas cost no write; repair is one atomic ``write_txn`` that
    bumps and re-creates triggers (so the schema version moves). Never call from a reader."""
    master = _master(conn)
    if all(_expected(conn, k, master) is None or _intact(conn, k, master) for k in KINDS):
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        master = _master(conn)
        for kind in KINDS:
            wanted = _expected(conn, kind, master)
            if wanted is None:
                continue  # older/malformed schema: readers stay literal
            found = _ours(master, kind)
            if kind.table in found and found[kind.table] != kind.ddl:
                continue  # foreign counter: never replace or certify
            if kind.table not in found:
                conn.execute(kind.ddl)
            had_row = conn.execute(f"SELECT 1 FROM {kind.table} WHERE id = 1").fetchone() is not None
            if found == wanted and had_row:
                continue
            conn.execute(f"INSERT OR IGNORE INTO {kind.table} VALUES (1, 0)")
            conn.execute(kind.bump)
            for name in set(found) - {kind.table} - set(wanted):
                conn.execute(f"DROP TRIGGER {_q(name)}")
            for name, sql in wanted.items():
                if name == kind.table:
                    continue
                if found.get(name) != sql or not had_row:
                    # A lost counter may restart at a cached value: re-creating triggers moves the
                    # schema version so an unseen gap cannot certify older rows.
                    conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                    conn.execute(sql)


def _read(conn: sqlite3.Connection, identity: tuple, kind: Kind):
    opened = not conn.in_transaction
    try:
        if opened:
            conn.execute("BEGIN")
        schema = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        master = _master(conn)
        if (kind in (COUNTS, LINKS) and _foreign_containment(master)) or not _intact_readonly(conn, kind, master):
            return None
        row = conn.execute(f"SELECT revision FROM {kind.table} WHERE id = 1").fetchone()
        return None if row is None else (*identity, int(row[0]), schema)
    except sqlite3.Error:
        return None
    finally:
        if opened and conn.in_transaction:
            conn.execute("ROLLBACK")


def _intact_readonly(conn, kind: Kind, master: dict) -> bool:
    wanted = _expected(conn, kind, master)
    return wanted is not None and _ours(master, kind) == wanted


def read_counts_revision(conn: sqlite3.Connection, identity: tuple) -> tuple | None:
    """Counts revision in the caller's snapshot (or its own read-only one); ``None`` = read literally."""
    return _read(conn, identity, COUNTS)


def read_attention_revision(conn: sqlite3.Connection, identity: tuple) -> tuple | None:
    """Attention revision, independent of the counts counter; ``None`` = read literally."""
    return _read(conn, identity, ATTENTION)


def content_key(path, part: str) -> tuple:
    """Fresh coverage/restore-aware holder key. Unknown never compares equal, and never writes."""
    from hermes_cli import kanban_activity_revision as revision
    from hermes_cli import kanban_session_activity as activity

    kind = {'counts': COUNTS, 'attention': ATTENTION, 'links': LINKS}[part]
    unknown = ('unknown', object())
    lock = revision.probe_lock(path)
    if not lock.acquire(timeout=0.05):
        return unknown
    try:
        before = revision.file_stat(path)[0]
        if before is None:
            return unknown
        with activity.cached_ro(path, busy_budget=0) as conn:
            value = _read(conn, (conn, before), kind)
        return unknown if value is None or revision.file_stat(path)[0] != before else ('part', part, *value)
    except (OSError, sqlite3.Error):
        return unknown
    finally:
        lock.release()
