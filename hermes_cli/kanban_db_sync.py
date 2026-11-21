"""Client-side seams for the central Kanban sync service (core patch 0193).

Everything here is inert unless ``kanban.sync.enabled`` is true when a board
file is first opened. With the flag off no sync table or trigger is created
(and triggers left by an earlier enable are dropped), so every kanban code path
behaves exactly as before.

With the flag on, each board file gets:

- ``kanban_sync_state`` (one row: mode ``off|shadow|primary``, feed cursor,
  ``applying`` echo guard, the run uid a granted lease reserved),
  ``kanban_sync_outbox`` and ``kanban_sync_tombstones``;
- a ``uid`` on comments/events/runs/attachments and the lease mirror columns on
  ``tasks``;
- ``AFTER INSERT/UPDATE/DELETE`` triggers that copy synced columns into the
  outbox inside the writer's own transaction. They are generated from
  ``PRAGMA table_info`` minus the declared local-only columns, so a column an
  upstream migration adds is captured by default; ``undeclared_columns`` names
  it so a test forces the synced/local decision.

The network side (outbox drain, change feed, enrollment) is not here: it lives
in a plugin on top of :func:`apply_remote_changes` and :func:`set_lease_client`.
Design: hermes-mods ``core/kanban-central-service.md``.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing

_log = logging.getLogger(__name__)

MODES = ("off", "shadow", "primary")
# A worker whose lease cannot be renewed is stopped this long before the
# server-side expiry, so it is gone before another gateway can be granted the card.
LEASE_SAFETY_MARGIN_SECONDS = 60
# [daveey] How long past its expiry a lapsed lease can still be ours while the Board cannot be asked. Matches the
# Board's DARK_HOLD_MAX_MS: the Board keeps the lease of a holder that went silent (a laptop asleep, offline) that
# long, so a worker frozen by sleep, or woken in a networkless maintenance wake, is not stopped on our clock alone.
LEASE_DARK_HOLD_SECONDS = 12 * 3600
# [daveey] Retry spacing for a lease that has already lapsed (see ``_renewal_due``).
LAPSED_RENEW_RETRY_SECONDS = 30

# Columns that travel to the service. Integer cross-references travel as uids
# (``REF_COLUMNS``); identity columns (``KEY_COLUMNS``) are the row key, never a field.
SYNCED_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({
        "title", "body", "assignee", "status", "priority", "created_by", "created_at", "started_at",
        "completed_at", "workspace_kind", "branch_name", "tenant", "result", "idempotency_key",
        "consecutive_failures", "last_failure_error", "max_runtime_seconds", "current_run_id",
        "workflow_template_id", "current_step_key", "skills", "requires", "model_override",
        "provider_override", "model_pin_mandatory", "reasoning_effort", "max_retries", "goal_mode", "goal_max_turns",
        "block_kind", "human_request", "block_recurrences", "completion_contract", "runs_on", "placement_policy",
        "admission_class", "admission_frees", "review_required",
    }),
    "task_links": frozenset({"parent_id", "child_id"}),
    "task_comments": frozenset({"task_id", "author", "body", "created_at"}),
    "task_events": frozenset({"task_id", "run_id", "kind", "payload", "created_at"}),
    "task_runs": frozenset({
        "task_id", "profile", "step_key", "status", "max_runtime_seconds", "started_at", "ended_at",
        "outcome", "summary", "metadata", "error",
    }),
    "task_attachments": frozenset({
        "task_id", "filename", "content_type", "size", "uploaded_by", "created_at", "blob_key",
    }),
    # Which chats a card belongs to (created / shepherd), on any gateway (RFC "Retiring the origin ledger").
    "task_chat_links": frozenset({
        "task_id", "chat_gateway", "chat_profile", "chat_session", "kind", "released", "created_at",
    }),
}
# Machine-local: host:pid claimers, PIDs, local paths, local session ids and the
# read-only lease mirror. Stored locally, never pushed, never overwritten by a pull.
LOCAL_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({
        "claim_lock", "claim_expires", "worker_pid", "worker_started_at", "last_heartbeat_at",
        "workspace_path", "session_id", "project_id", "sync_version", "field_hlc", "lease_holder",
        "lease_expires", "lease_version", "admission_origin",
    }),
    "task_links": frozenset(),
    # ``origin_gateway``: the sending gateway as this gateway saw it (relay sender); peers keep their own.
    "task_comments": frozenset({"origin_gateway"}),
    "task_events": frozenset({"origin_gateway"}),
    # ``sync_version``: the Board version of the row this gateway last applied or had acknowledged (the fence that
    # keeps an older copy of a run from the feed off a newer local row).
    "task_runs": frozenset({"claim_lock", "claim_expires", "worker_pid", "worker_started_at", "last_heartbeat_at",
                            "sync_version", "admission_pool", "admission_class", "admission_origin"}),
    "task_attachments": frozenset({"stored_path"}),
    "task_chat_links": frozenset(),
}
# Set by the service, never by a gateway: applied from the feed, never captured.
# ``ready_since``: when the card last became ready (ms), for the Board's ``home_grace`` placement rule.
# ``workspace_source``/``workspace_custody``: Board-written only (``publish_source``, custody hooks); a push is refused.
SERVER_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({"created_on", "alarm_source", "ready_since", "workspace_source", "workspace_custody", "containment_episode"})}
JSON_SERVER_COLUMNS: dict[str, frozenset[str]] = {"tasks": frozenset({"workspace_source", "workspace_custody"})}
# Old columns an ADD-first rename migration keeps (``kanban_db_connect._RENAMED_TASK_COLUMNS``: the new column is
# added and filled from the old one, which stays). Every board older than the rename holds them; only
# ``Task.from_row``'s pre-migration fallbacks read them, and they never travel. Known, so the schema-ahead guard
# (``undeclared_columns``) does not read an OLDER schema's leftovers as a newer core's columns. [daveey]
RETIRED_COLUMNS: dict[str, frozenset[str]] = {"tasks": frozenset({"spawn_failures", "last_spawn_error"})}
KEY_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({"id"}),
    "task_links": frozenset(),
    **{t: frozenset({"id", "uid"}) for t in ("task_comments", "task_events", "task_runs", "task_attachments",
                                             "task_chat_links")},
}
REF_COLUMNS: dict[tuple[str, str], str] = {
    ("tasks", "current_run_id"): "current_run_uid",
    ("task_events", "run_id"): "run_uid",
}
SYNCED_TABLES = tuple(SYNCED_COLUMNS)
# Wire-only fields: carried by an outbox op and sent, never a column (so never in ``SYNCED_COLUMNS``, which
# ``undeclared_columns``, ``_row_fields`` and the import path read as real columns). ``source_receipt`` rides
# the status-leaving tasks op (``kanban_source_receipt``); an old Board's refusal drops it with no omission.
VIRTUAL_WIRE_FIELDS: dict[str, frozenset[str]] = {"tasks": frozenset({"source_receipt"})}
# Synced columns core keeps in Unix SECONDS (``int(time.time())``). The service's own clocks (leases, HLCs, the feed,
# tombstones) are milliseconds; a millisecond value in one of these columns makes every ``now - started_at`` reader
# wrong by 1000x (2026-10-27: the Board's lease grants wrote ``task_runs.started_at`` in ms and
# ``coach_fixer.utc_day`` crashed on "year 58792"). The apply path normalizes them, whatever a peer sends.
SECONDS_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({"created_at", "started_at", "completed_at"}),
    "task_runs": frozenset({"started_at", "ended_at"}),
    "task_comments": frozenset({"created_at"}),
    "task_events": frozenset({"created_at"}),
    "task_attachments": frozenset({"created_at"}),
    "task_chat_links": frozenset({"created_at"}),
}
# Above this a timestamp is milliseconds: 1e11 s is the year 5138, 1e11 ms is 1973.
MS_THRESHOLD = 100_000_000_000


def to_seconds(value: Any) -> Any:
    """A seconds-column value from the wire: a millisecond number becomes seconds, anything else is unchanged."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > MS_THRESHOLD:
        return int(value // 1000)
    return value


def repair_ms_timestamps(conn: sqlite3.Connection) -> int:
    """Rewrite millisecond values in ``SECONDS_COLUMNS`` to seconds; returns the number of cells fixed.

    Runs with ``applying`` set, so nothing is captured: the service repairs its own copy, and any later write of a
    row sends the repaired value. The caller holds the write transaction."""
    fixed = 0
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.execute("UPDATE kanban_sync_state SET applying = 1")
    try:
        for table, columns in SECONDS_COLUMNS.items():
            if table not in present:
                continue
            have = set(table_columns(conn, table))
            for column in sorted(columns & have):
                fixed += conn.execute(
                    f"UPDATE {table} SET {column} = CAST({column} / 1000 AS INTEGER) WHERE {column} > ?",
                    (MS_THRESHOLD,)).rowcount
    finally:
        conn.execute("UPDATE kanban_sync_state SET applying = 0")
    return fixed
_UID_TABLES = ("task_comments", "task_events", "task_runs", "task_attachments", "task_chat_links")
# Append-only rows leave only with their task; a delete trigger there would echo
# every cascade row of a task delete the service already cascades itself.
# Chat links are not append-only: a local delete with no trigger left the Board's row behind forever, a shadow-only
# ``task_chat_links`` mismatch nothing could heal (238 retired placement links on the hub, 2026-10-30). No local
# path cascades them with a task delete, and evict deletes under ``applying``, so the trigger echoes nothing.
_DELETE_CAPTURED = ("tasks", "task_links", "task_attachments", "task_chat_links")

_NOW_MS_SQL = "CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
# UUIDv7 inline (no subquery: SQLite may evaluate an uncorrelated subquery once per statement).
_UUID7_SQL = (
    f"printf('%08x-%04x-7%03x-%x%03x-%012x', ({_NOW_MS_SQL}) >> 16, ({_NOW_MS_SQL}) & 65535, "
    "abs(random()) % 4096, 8 + abs(random()) % 4, abs(random()) % 4096, abs(random()) % 281474976710656)"
)
_NOT_APPLYING = "COALESCE((SELECT applying FROM kanban_sync_state LIMIT 1), 0) = 0"

SYNC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanban_sync_outbox (
    op_id        TEXT PRIMARY KEY,
    table_name   TEXT NOT NULL,
    row_uid      TEXT NOT NULL,
    op           TEXT NOT NULL,
    fields       TEXT NOT NULL,
    base_version INTEGER,
    hlc          TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    state        TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON kanban_sync_outbox(state, created_at);
-- One local capture descriptor per extant task, not an op ledger.
CREATE TABLE IF NOT EXISTS kanban_sync_status_origins (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    origin TEXT,
    status TEXT
);
-- First-send synthetic parents are pending transport, not a permanent receipt ledger.
CREATE TABLE IF NOT EXISTS kanban_sync_parent_envelopes (
    task_id TEXT PRIMARY KEY,
    wire TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kanban_sync_omissions (
    original_op_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    row_uid TEXT NOT NULL,
    field TEXT NOT NULL,
    compat_op_id TEXT NOT NULL,
    restore_op_id TEXT,
    state TEXT NOT NULL DEFAULT 'held'
);
CREATE INDEX IF NOT EXISTS idx_sync_omissions_row ON kanban_sync_omissions(table_name, row_uid);
CREATE TABLE IF NOT EXISTS kanban_sync_remote_parents (
    parent_id    TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    refreshed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kanban_sync_tombstones (
    table_name TEXT NOT NULL,
    row_uid    TEXT NOT NULL,
    deleted_at INTEGER NOT NULL,
    PRIMARY KEY (table_name, row_uid)
);
CREATE TABLE IF NOT EXISTS task_chat_links (
    id           INTEGER PRIMARY KEY,
    uid          TEXT,
    task_id      TEXT NOT NULL,
    chat_gateway TEXT NOT NULL,
    chat_profile TEXT,
    chat_session TEXT NOT NULL,
    kind         TEXT NOT NULL,
    released     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_chat_links_chat ON task_chat_links(chat_gateway, chat_session);
-- [daveey] Per-card lookups (the owner's ``_owner_hub_links``, ``proven_foreign_chats``, the released-sibling
-- EXISTS of ``_read_replica_links``) scanned every link without it. An index is no column: ``SchemaAhead`` and the
-- wire format only compare columns, so an older core syncs a file that has it. A process's first open of each board
-- file and replica runs this script (``install_or_remove``), so it is also the migration of existing files.
CREATE INDEX IF NOT EXISTS idx_task_chat_links_task ON task_chat_links(task_id);
CREATE TABLE IF NOT EXISTS kanban_sync_state (
    board_id        TEXT PRIMARY KEY,
    cursor          INTEGER NOT NULL DEFAULT 0,
    gateway_id      TEXT NOT NULL,
    hlc_last        TEXT NOT NULL,
    mode            TEXT NOT NULL,
    applying        INTEGER NOT NULL DEFAULT 0,
    pending_run_uid TEXT
);
"""
_SYNC_COLUMN_DDL = (
    *((t, "uid", "uid TEXT") for t in _UID_TABLES),
    ("task_attachments", "blob_key", "blob_key TEXT"),
    ("tasks", "sync_version", "sync_version INTEGER"),
    ("task_runs", "sync_version", "sync_version INTEGER"),
    ("tasks", "field_hlc", "field_hlc TEXT"),
    ("tasks", "lease_holder", "lease_holder TEXT"),
    ("tasks", "lease_expires", "lease_expires INTEGER"),
    ("tasks", "lease_version", "lease_version INTEGER"),
    ("tasks", "created_on", "created_on TEXT"),
    ("tasks", "alarm_source", "alarm_source TEXT"),
    ("tasks", "ready_since", "ready_since INTEGER"),
    # Placement (RFC "Placement"): JSON list of labels the claiming machine must all carry; NULL = default.
    ("tasks", "runs_on", "runs_on TEXT"),
    # workspace_source v1: the Board's manifest and custody (JSON), pulled read-only, never captured or pushed.
    ("tasks", "workspace_source", "workspace_source TEXT"),
    ("tasks", "workspace_custody", "workspace_custody TEXT"),
    ("kanban_sync_outbox", "retry_after", "retry_after INTEGER"),
    ("kanban_sync_outbox", "wire_json", "wire_json TEXT"),
    ("kanban_sync_outbox", "status_origin", "status_origin TEXT"),
    ("kanban_sync_status_origins", "status", "status TEXT"),
    # 1 once repair_ms_timestamps ran on this file (the one-time repair of rows applied before the guard).
    ("kanban_sync_state", "seconds_repaired", "seconds_repaired INTEGER NOT NULL DEFAULT 0"),
    # Wall time of the last successful feed pull into this file (replicas): readers derive staleness from it. [daveey]
    ("kanban_sync_state", "pulled_at", "pulled_at REAL"),
    # Replicas: the ``pulled_at`` of the pull the live remote-parent mirror was last refreshed from. [daveey]
    ("kanban_sync_state", "mirror_pulled_at", "mirror_pulled_at REAL"),
    # Local-only round-robin position; refused revisions must not monopolize bounded batches.
    ("kanban_sync_state", "worker_reserve_cursor", "worker_reserve_cursor TEXT"),
)

# Resolved db paths whose triggers this process installed. Empty = no synced
# board was opened here, and every seam returns without touching SQLite.
_SYNCED_PATHS: set[str] = set()
_DISPATCHER_WAKE = threading.Event()
_RENEW_ATTEMPTS: dict[str, float] = {}


def uuid7() -> str:
    """Time-ordered UUIDv7 string (48-bit ms timestamp + 74 random bits)."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | (rand >> 68 & 0xFFF) << 64 | 0b10 << 62 | rand & ((1 << 62) - 1)
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# --- Config + install -----------------------------------------------------------------------------

def sync_config() -> Mapping[str, Any]:
    """``kanban.sync`` from the kanban root's ``config.yaml`` (empty when unset or unreadable).

    Kanban storage is shared by every profile on the host (``kanban_db.kanban_home``), so the switch is read
    there, never from the active profile: a worker running under a profile without the setting must not see
    sync as off and drop the board's capture triggers under the gateway."""
    try:
        from hermes_cli import kanban_db
        from hermes_cli.config_backend import config_exists, read_config_doc_readonly
        path = kanban_db.kanban_home() / "config.yaml"
        kanban = ((read_config_doc_readonly(path) if config_exists(path) else None) or {}).get("kanban") or {}
    except Exception as exc:  # health: allow BLE001 -- an unreadable config reads as sync off; connect() must not break
        _log.warning("kanban sync: cannot read config (%s: %s); sync stays off", type(exc).__name__, exc)
        return {}
    sync = kanban.get("sync") if isinstance(kanban, Mapping) else None
    return sync if isinstance(sync, Mapping) else {}


def sync_enabled() -> bool:
    """``kanban.sync.enabled`` (default False). An unreadable config reads as off,
    which is today's behaviour."""
    return bool(sync_config().get("enabled"))


SHADOW_DIR = "sync-shadow"
# Link-only boards: a board another gateway owns that this gateway's chats have cards on. The file holds no cards,
# only this gateway's chat links (pushed); its replica holds the whole board. Outside ``boards/``, so the dispatcher
# and the board list never see it.
LINKS_DIR = "sync-links"


def board_slug_for_path(db_path: str) -> str:
    """``<root>/kanban.db`` -> ``default``; ``<root>/kanban/boards/<slug>/kanban.db`` -> ``<slug>``;
    a shadow copy ``.../sync-shadow/<slug>.db`` -> ``<slug>``."""
    from pathlib import Path
    p = Path(db_path)
    if p.parent.name in (SHADOW_DIR, LINKS_DIR):
        return p.stem
    if p.name == "kanban.db" and p.parent.parent.name == "boards":
        return p.parent.name
    return "default"


def board_selected(db_path: str, config: Optional[Mapping[str, Any]] = None) -> bool:
    """``kanban.sync.boards`` lists the boards that sync (absent = every board). A shadow copy and a link-only board
    always do. ``config``: an already-read ``sync_config()`` (a caller checking many boards reads it once)."""
    from pathlib import Path
    if Path(db_path).parent.name in (SHADOW_DIR, LINKS_DIR):
        return True
    boards = (sync_config() if config is None else config).get("boards")
    if not isinstance(boards, (list, tuple)):
        return True
    return board_slug_for_path(db_path) in {str(b) for b in boards}


def _db_key(conn: sqlite3.Connection) -> Optional[str]:
    from hermes_cli.kanban_db_connect import _main_db_file
    return _main_db_file(conn)


# Local-only mirror (never captured, never pushed): the Board status of every parent a local link names that is not
# a local card, i.e. a card another gateway owns. ``kanban_db.parent_states`` reads it, so the promoter and the claim
# gate on the same parents the Board's own claim check does. Refreshed from the replica each sync round
# (``kanban_sync_client.refresh_remote_parents``). A parent the replica lacks has no row: it reads as missing. [daveey]
REMOTE_PARENTS_TABLE = "kanban_sync_remote_parents"


def has_remote_parents(conn: sqlite3.Connection) -> bool:
    """Whether this board file has the remote-parent mirror (synced boards only)."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (REMOTE_PARENTS_TABLE,)).fetchone() is not None


def refresh_remote_parents(live: sqlite3.Connection, replica: sqlite3.Connection, *,
                           scoped=None) -> list[str]:
    """Copy the replica's status of every foreign parent the live file's links name into the mirror; drop rows no
    link needs and rows the replica no longer has. Returns the parents whose status changed (the caller wakes the
    dispatcher so a released child is promoted on the next tick, not the next sweep). ``scoped``: the evidence scope
    the caller already read (the P2 gate stamps exactly the scope the mirror was built from)."""
    if not has_remote_parents(live):
        return []
    wanted = [r[0] for r in live.execute(
        "SELECT DISTINCT l.parent_id FROM task_links l LEFT JOIN tasks p ON p.id = l.parent_id WHERE p.id IS NULL")]
    from hermes_cli import kanban_move_parent_evidence as evidence
    from hermes_cli.kanban_db_connect import write_txn

    # Status and result must describe the same replica snapshot. A savepoint also
    # composes with callers already reading a transaction on this connection.
    replica.execute("SAVEPOINT move_parent_snapshot")
    try:
        found: dict[str, str] = {}
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            found.update({r[0]: r[1] for r in replica.execute(
                f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(chunk))})", chunk)})
        values, evidence_changed = evidence.prepare(live, replica, wanted, scoped=scoped)
    finally:
        replica.execute("RELEASE move_parent_snapshot")
    current = {r[0]: r[1] for r in live.execute(f"SELECT parent_id, status FROM {REMOTE_PARENTS_TABLE}")}
    changed = {k for k in set(current) | set(found) if current.get(k) != found.get(k)}
    if not changed and not evidence_changed:
        return []  # An unchanged sync round must not acquire a primary write lock.
    with write_txn(live):
        if evidence_changed:
            evidence.apply(live, values)
        if changed:
            now = int(time.time())
            live.execute(f"DELETE FROM {REMOTE_PARENTS_TABLE}")
            live.executemany(f"INSERT INTO {REMOTE_PARENTS_TABLE} (parent_id, status, refreshed_at) VALUES (?, ?, ?)",
                             [(pid, st, now) for pid, st in found.items()])
    return sorted(changed | set(evidence_changed))


def install_or_remove(conn: sqlite3.Connection, db_path: str) -> None:
    """Init-time seam: with the flag on, create the sync tables and (unless the
    board was detached to ``off``) install the capture triggers; with it off,
    drop any capture trigger an earlier enable left in the file. A board not listed
    in ``kanban.sync.boards`` is treated as off."""
    if not sync_enabled() or not board_selected(db_path):
        _SYNCED_PATHS.discard(db_path)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
                        "AND name LIKE 'kanban_sync_%' LIMIT 1").fetchone():
            if _attached(conn) and not _explicitly_off(db_path):
                # [daveey] A process whose kanban home has no sync setting (a probe with a temp HERMES_HOME that
                # inherited a worker's HERMES_KANBAN_DB) must not detach a board the Board service owns: the gap it
                # opened left a claim's run uid uncaptured, so the Board's echo made a second run row and the next
                # local run insert failed UNIQUE(task_runs.uid). Detaching takes `kanban.sync.enabled: false` (or a
                # `boards` list without this board) in that home, or `set_mode(conn, "off")`.
                _log.warning("kanban sync: %s is attached to the Board service but this process's kanban home "
                             "(%s) does not enable sync; leaving its capture triggers in place", db_path, _home_hint())
                return
            drop_triggers(conn)
        return
    apply_sync_schema_steps(conn)
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):  # one transaction: no other writer can land between dropping and recreating a trigger
        enabled_now = conn.execute(
            "INSERT OR IGNORE INTO kanban_sync_state (board_id, gateway_id, hlc_last, mode) "
            "SELECT ?, ?, '', 'shadow' WHERE NOT EXISTS (SELECT 1 FROM kanban_sync_state)",
            (f"local:{uuid7()}", f"gw_{uuid7()}"),
        ).rowcount > 0
        if enabled_now:  # [daveey] peers may now receive this board's cards: no unsynced freshness marker survives
            from hermes_cli.kanban_wake_episode import forget_fresh
            forget_fresh(conn)
        state = conn.execute("SELECT mode, board_id FROM kanban_sync_state").fetchone()
        if state[0] == "off":
            drop_triggers(conn)
            _SYNCED_PATHS.discard(db_path)
            return
        # Rows without a uid were written while no trigger captured them: before the first enable (import uploads
        # those), or, on a board already attached to the service, in a capture gap. Give them their identity, and
        # queue the gap's rows for upload so they cannot silently diverge.
        attached = not str(state[1]).startswith("local:")
        for table in _UID_TABLES:
            missed = [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE uid IS NULL")] if attached else []
            conn.execute(f"UPDATE {table} SET uid = {_UUID7_SQL} WHERE uid IS NULL")
            if missed:
                _queue_rows(conn, table, missed)
                _log.warning("kanban sync: %d %s row(s) were written without capture; queued for upload", len(missed), table)
        install_triggers(conn)
        if not conn.execute("SELECT seconds_repaired FROM kanban_sync_state").fetchone()[0]:
            fixed = repair_ms_timestamps(conn)
            conn.execute("UPDATE kanban_sync_state SET seconds_repaired = 1")
            if fixed:
                _log.warning("kanban sync: %d millisecond timestamp(s) rewritten to seconds", fixed)
    _SYNCED_PATHS.add(db_path)


def apply_sync_schema_steps(conn: sqlite3.Connection) -> None:
    """Every sync table/column/index step of :func:`install_or_remove`. ``kanban_schema_owner`` replays it on an
    in-memory DB to judge a synced board "at par", so a sync column added here is never missed there. [daveey]"""
    conn.executescript(SYNC_SCHEMA_SQL)
    for table, column, ddl in _SYNC_COLUMN_DDL:
        _add_column_if_missing(conn, table, column, ddl)
    for table in _UID_TABLES:
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_uid ON {table}(uid)")
    # task_runs.sync_version joins the board's run-bookkeeping covering index.
    from hermes_cli.kanban_db_connect import ensure_run_bookkeeping_index
    ensure_run_bookkeeping_index(conn)


def live_without_install(conn: sqlite3.Connection, db_path: str) -> bool:
    """Whether :func:`register_without_install` registers ``db_path`` as synced: sync is on for the board and the
    installed core attached it (state row not ``off``, capture triggers present). [daveey]"""
    try:
        return bool(sync_enabled() and board_selected(db_path)
                    and conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
                                     "AND name LIKE 'kanban_sync_%' LIMIT 1").fetchone() is not None
                    and (conn.execute("SELECT mode FROM kanban_sync_state LIMIT 1").fetchone() or ("off",))[0] != "off")
    except sqlite3.OperationalError:  # no kanban_sync_state: the installed core never enabled sync here
        return False


def register_without_install(conn: sqlite3.Connection, db_path: str) -> None:
    """:func:`install_or_remove` for a core that may not change this board's schema (``kanban_schema_owner``: a
    non-installed checkout on a board already at its schema). Writes nothing: the installed core owns the sync
    tables and capture triggers. When it has attached the board (:func:`live_without_install`), register the path,
    so :func:`board_mode` reads the real mode and this process mints Board ids and takes Board leases like the
    installed core. [daveey]"""
    live = live_without_install(conn, db_path)
    (_SYNCED_PATHS.add if live else _SYNCED_PATHS.discard)(db_path)


def _attached(conn: sqlite3.Connection) -> bool:
    """The file is bound to a Board-service board (not a ``local:`` id) and not detached to ``off``."""
    try:
        row = conn.execute("SELECT board_id, mode FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and not str(row[0]).startswith("local:") and row[1] != "off"


def _explicitly_off(db_path: str) -> bool:
    """The kanban home's config says sync is off for this board, as opposed to saying nothing (no config, no
    ``kanban.sync``, unreadable)."""
    cfg = sync_config()
    if "enabled" in cfg and not cfg.get("enabled"):
        return True
    return bool(cfg.get("enabled")) and not board_selected(db_path)


def _home_hint() -> str:
    try:
        from hermes_cli import kanban_db
        return str(kanban_db.kanban_home())
    except Exception:  # health: allow BLE001 -- log hint only
        return "?"


def _queue_rows(conn: sqlite3.Connection, table: str, ids: list[int]) -> None:
    """Queue full-row upserts for ``ids`` (rows a capture trigger never saw)."""
    fields = "json_object(" + ", ".join(
        f"'{_wire_name(table, c)}', json({_wire_value(table, c, 's')})" for c in captured_columns(conn, table)) + ")"
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        conn.execute(
            "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
            f"SELECT {_UUID7_SQL}, '{table}', s.uid, 'upsert', {fields}, NULL, '', {_NOW_MS_SQL} "
            f"FROM {table} AS s WHERE s.id IN ({','.join('?' * len(part))})", part)


def drop_triggers(conn: sqlite3.Connection) -> None:
    # Detached/downgraded writers no longer invalidate current-row provenance.
    # Reinstallation calls this too, covering old-core removal of our triggers.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_status_origins'").fetchone():
        conn.execute("DELETE FROM kanban_sync_status_origins")
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'kanban_sync_%'")]
    for name in names:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def captured_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Columns the triggers capture: everything but local-only, server-set and key columns."""
    skip = LOCAL_COLUMNS[table] | KEY_COLUMNS[table] | SERVER_COLUMNS.get(table, frozenset())
    return [c for c in table_columns(conn, table) if c not in skip]


def undeclared_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Per synced table, columns that are neither synced, local-only, a key nor retired:
    a migration added them and nobody decided whether they leave the gateway."""
    out: dict[str, set[str]] = {}
    for table in SYNCED_TABLES:
        known = (SYNCED_COLUMNS[table] | LOCAL_COLUMNS[table] | KEY_COLUMNS[table]
                 | SERVER_COLUMNS.get(table, frozenset()) | RETIRED_COLUMNS.get(table, frozenset()))
        extra = set(table_columns(conn, table)) - known
        if extra:
            out[table] = extra
    return out


def _wire_name(table: str, column: str) -> str:
    return REF_COLUMNS.get((table, column), column)


def _wire_value(table: str, column: str, ref: str) -> str:
    """SQL for one captured value as JSON text (``ref`` is ``NEW`` or a row alias).
    JSON cannot hold a BLOB, and a capture trigger must never fail the writer's
    statement, so a BLOB cell travels as ``{"$blob": "<hex>"}``."""
    if (table, column) in REF_COLUMNS:
        return f"json_quote((SELECT r.uid FROM task_runs AS r WHERE r.id = {ref}.{column}))"
    value = f"{ref}.{column}"
    return f"CASE WHEN typeof({value}) = 'blob' THEN json_object('$blob', hex({value})) ELSE json_quote({value}) END"


def _from_wire(value: Any) -> Any:
    if isinstance(value, Mapping) and set(value) == {"$blob"}:
        return bytes.fromhex(value["$blob"])
    return value


def _row_uid_sql(table: str, ref: str) -> str:
    if table == "tasks":
        return f"{ref}.id"
    if table == "task_links":
        return f"{ref}.parent_id || '>' || {ref}.child_id"
    return f"{ref}.uid"


def _outbox_insert(table: str, op: str, row_uid: str, fields_sql: str, base_version: str = "NULL") -> str:
    return (
        "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
        f"VALUES ({_UUID7_SQL}, '{table}', {row_uid}, '{op}', {fields_sql}, {base_version}, '', {_NOW_MS_SQL});"
    )


def _insert_trigger(table: str, columns: list[str]) -> str:
    name = f"kanban_sync_{table}_ins"
    if table in _UID_TABLES:
        uid = _UUID7_SQL
        prelude = ""
        if table == "task_runs":
            uid = f"COALESCE((SELECT pending_run_uid FROM kanban_sync_state LIMIT 1), {_UUID7_SQL})"
            prelude = "UPDATE kanban_sync_state SET pending_run_uid = NULL WHERE pending_run_uid IS NOT NULL;"
        fields = "json_object(" + ", ".join(f"'{_wire_name(table, c)}', json({_wire_value(table, c, 's')})" for c in columns) + ")"
        body = (
            f"UPDATE {table} SET uid = {uid} WHERE id = NEW.id AND uid IS NULL; {prelude}"
            "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
            f"SELECT {_UUID7_SQL}, '{table}', s.uid, 'upsert', {fields}, NULL, '', {_NOW_MS_SQL} "
            f"FROM {table} AS s WHERE s.id = NEW.id;"
        )
    else:
        fields = "json_object(" + ", ".join(f"'{_wire_name(table, c)}', json({_wire_value(table, c, 'NEW')})" for c in columns) + ")"
        body = _outbox_insert(table, "upsert", _row_uid_sql(table, "NEW"), fields)
        if table == "task_links":  # add-wins: re-linking clears an earlier removal
            body = (f"DELETE FROM kanban_sync_tombstones WHERE table_name = 'task_links' "
                    f"AND row_uid = {_row_uid_sql(table, 'NEW')}; " + body)
    return f"CREATE TRIGGER {name} AFTER INSERT ON {table} WHEN {_NOT_APPLYING} BEGIN {body} END"


def _update_trigger(table: str, columns: list[str]) -> str:
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in columns)
    pairs = " UNION ALL ".join(
        f"SELECT '{_wire_name(table, c)}' AS k, {_wire_value(table, c, 'NEW')} AS v WHERE OLD.{c} IS NOT NEW.{c}"
        for c in columns
    )
    if table == "tasks":  # the virtual receipt rides the status-leaving op (kanban_source_receipt)
        from hermes_cli.kanban_source_receipt import trigger_pair
        pairs += f" UNION ALL {trigger_pair()}"
    fields = f"(SELECT json_group_object(k, json(v)) FROM ({pairs}))"
    base = "NEW.sync_version" if table == "tasks" else "NULL"
    body = _outbox_insert(table, "upsert", _row_uid_sql(table, "NEW"), fields, base)
    return (f"CREATE TRIGGER kanban_sync_{table}_upd AFTER UPDATE ON {table} "
            f"WHEN {_NOT_APPLYING} AND ({changed}) BEGIN {body} END")


def _delete_trigger(table: str) -> str:
    when = _NOT_APPLYING
    if table == "task_links":  # foreign parents are absent locally; the child owns the edge
        when += " AND EXISTS (SELECT 1 FROM tasks WHERE id = OLD.child_id)"
    elif table == "task_attachments":
        when += " AND EXISTS (SELECT 1 FROM tasks WHERE id = OLD.task_id)"
    uid = _row_uid_sql(table, "OLD")
    body = (
        _outbox_insert(table, "delete", uid, "'{}'")
        + " INSERT OR REPLACE INTO kanban_sync_tombstones (table_name, row_uid, deleted_at) "
        f"VALUES ('{table}', {uid}, {_NOW_MS_SQL});"
    )
    return f"CREATE TRIGGER kanban_sync_{table}_del AFTER DELETE ON {table} WHEN {when} BEGIN {body} END"


def wanted_triggers(conn: sqlite3.Connection) -> dict[str, str]:
    """Trigger name -> CREATE statement, generated from the live column list."""
    out: dict[str, str] = {
        "kanban_sync_origin_invalidate": "CREATE TRIGGER kanban_sync_origin_invalidate AFTER UPDATE OF status ON tasks "
        "WHEN OLD.status IS NOT NEW.status BEGIN DELETE FROM kanban_sync_status_origins WHERE task_id=NEW.id; END",
        "kanban_sync_origin_delete": "CREATE TRIGGER kanban_sync_origin_delete AFTER DELETE ON tasks "
        "BEGIN DELETE FROM kanban_sync_status_origins WHERE task_id=OLD.id; END",
    }
    for table in SYNCED_TABLES:
        columns = captured_columns(conn, table)
        sqls = [_insert_trigger(table, columns)]
        if table != "task_links":
            sqls.append(_update_trigger(table, columns))
        if table in _DELETE_CAPTURED:
            sqls.append(_delete_trigger(table))
        for sql in sqls:
            out[sql.split()[2]] = sql
    return out


def install_triggers(conn: sqlite3.Connection) -> None:
    """(Re)generate every capture trigger from the live column list. A no-op when the installed set already
    matches, so ordinary process starts never touch them; callers hold a write transaction so a rebuild has no
    window in which another process's write goes uncaptured."""
    from hermes_cli import kanban_source_receipt
    kanban_source_receipt.ensure_schema(conn)  # the tasks UPDATE trigger reads its pending table
    wanted = wanted_triggers(conn)
    have = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'kanban_sync_%'"))
    if have == wanted:
        return
    drop_triggers(conn)
    for sql in wanted.values():
        conn.execute(sql)


# --- Per-connection state -------------------------------------------------------------------------

def board_mode(conn: sqlite3.Connection) -> str:
    """``off`` unless this process installed sync on the connection's file."""
    if not _SYNCED_PATHS or _db_key(conn) not in _SYNCED_PATHS:
        return "off"
    row = conn.execute("SELECT mode FROM kanban_sync_state LIMIT 1").fetchone()
    return row[0] if row else "off"


def _state(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT board_id, gateway_id, cursor FROM kanban_sync_state LIMIT 1").fetchone()


def set_mode(conn: sqlite3.Connection, mode: str) -> None:
    """Switch the board's sync mode; ``off`` drops the capture triggers."""
    if mode not in MODES:
        raise ValueError(f"unknown kanban sync mode {mode!r}")
    if mode == "off":
        from pathlib import Path
        from hermes_cli.kanban_sync_guard import board_sync_lock
        path = _db_key(conn)
        if path is None:
            raise ValueError("detach requires an on-disk board")
        with board_sync_lock(Path(path)):
            _set_mode(conn, mode)
    else:
        _set_mode(conn, mode)


def _set_mode(conn: sqlite3.Connection, mode: str) -> None:
    from hermes_cli.kanban_db_connect import write_txn
    key = _db_key(conn)
    with write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET mode = ?", (mode,))
        if mode == "off":
            drop_triggers(conn)
        else:
            install_triggers(conn)
    if key:
        (_SYNCED_PATHS.discard if mode == "off" else _SYNCED_PATHS.add)(key)


def set_identity(conn: sqlite3.Connection, board_id: str, gateway_id: str) -> None:
    """Bind the board file to its service board and this gateway's service name (the name the service records in
    ``origin_gateway``, ``created_on`` and lease holders, so echo and lease checks compare like with like)."""
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET board_id = ?, gateway_id = ?", (board_id, gateway_id))


def sync_state(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """The board's sync row (None when sync never ran on this file)."""
    try:
        row = conn.execute("SELECT * FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def mark_pulled(conn: sqlite3.Connection, at: Optional[float] = None) -> None:
    """Stamp a successful feed pull (``pulled_at``), so readers of a replica can tell a fresh copy from an old one
    without asking the network. [daveey]"""
    from hermes_cli.kanban_db_connect import write_txn
    stamp = time.time() if at is None else at
    with write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET pulled_at = ? WHERE pulled_at IS NOT ?", (stamp, stamp))


def mark_mirrored(replica: sqlite3.Connection, board: str = "") -> None:
    """Record on the replica that the live remote-parent mirror now reflects its last pull (``mirror_pulled_at`` =
    ``pulled_at``). The round stamps ``pulled_at`` before it refreshes the mirror, so a fresh ``pulled_at`` alone
    does not prove the mirror is current; the missing-parent detector also needs this one. A failed stamp only
    leaves the detector deferring (fail closed), so it is logged, not raised. [daveey]"""
    from hermes_cli.kanban_db_connect import write_txn
    try:
        with write_txn(replica):
            replica.execute("UPDATE kanban_sync_state SET mirror_pulled_at = pulled_at "
                            "WHERE mirror_pulled_at IS NOT pulled_at")
    except sqlite3.Error as exc:
        _log.warning("kanban sync: %s remote-parent mirror not stamped (%s)", board, exc)


def new_task_id(conn: sqlite3.Connection) -> str:
    """Synced boards: ``t_`` + 16 hex (48-bit ms timestamp + 16 random bits), safe
    across gateways and offline creation; otherwise the classic 8-hex id."""
    from hermes_cli import kanban_db as _kb
    if board_mode(conn) == "off":
        return _kb._new_task_id()
    return f"t_{time.time_ns() // 1_000_000 & ((1 << 48) - 1):012x}{secrets.token_hex(2)}"


# --- Leases ---------------------------------------------------------------------------------------

class LeaseUnavailable(Exception):
    """The lease service could not be reached (offline, timeout, not enrolled).
    Clients raise this (or let a socket ``OSError`` through) for every transport failure."""


_CLIENT_ERRORS = (LeaseUnavailable, OSError)


@dataclass(frozen=True)
class Lease:
    granted: bool
    run_uid: str
    expires_ms: Optional[int] = None
    holder: Optional[str] = None
    reason: Optional[str] = None
    version: Optional[int] = None
    # workspace_source v1: the grant response row's manifest/custody (``None``: an old Board sent no snapshot keys).
    source_snapshot: Optional[Any] = None


class LeaseClient(Protocol):
    def acquire(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str, ttl_s: int,
                lane: str = "ready", source_protocol: Optional[int] = None) -> Lease: ...

    def renew(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str, ttl_s: int) -> Lease: ...

    def release(self, *, board_id: str, task_uid: str, run_uid: str, gateway_id: str,
                unused_grant: bool = False) -> None: ...


_LEASE_CLIENT: Optional[LeaseClient] = None


def set_lease_client(client: Optional[LeaseClient]) -> Optional[LeaseClient]:
    """Install the process's lease client (the sync plugin); returns the previous one."""
    global _LEASE_CLIENT
    previous, _LEASE_CLIENT = _LEASE_CLIENT, client
    return previous


def _client() -> LeaseClient:
    """The registered lease client, or one built from this host's hub enrollment on first use: workers renew
    their lease from their own process (the ``kanban_heartbeat`` tool), not the gateway's."""
    global _LEASE_CLIENT
    if _LEASE_CLIENT is None and sync_enabled():
        try:
            from hermes_cli import kanban_sync_client as ksc
            _LEASE_CLIENT = ksc.HubLeaseClient(ksc.Hub(ksc.load_identity()))
        except Exception as exc:  # health: allow BLE001 -- no enrollment / bad config: claims fail closed below
            _log.info("kanban sync: no lease client (%s: %s)", type(exc).__name__, exc)
    if _LEASE_CLIENT is None:
        raise LeaseUnavailable("no lease client registered")
    return _LEASE_CLIENT


class ClaimGate:
    """Outcome of asking for a lease before a claim CAS.

    Used as the OUTER context manager around the claim's ``write_txn`` so a
    lease granted for a CAS that then lost is released after the commit, never
    while the SQLite write lock is held."""

    def __init__(self, expires: int, *, deferred: bool = False, lease: Optional[Lease] = None,
                 ids: Optional[tuple[str, str, str]] = None) -> None:
        self.expires, self.deferred, self.lease, self._ids = expires, deferred, lease, ids
        self.consumed = False
        self.echo = None  # [daveey] proven own-grant echo, bound under the claim txn (kanban_grant_echo, review F5)

    def __enter__(self) -> ClaimGate:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.lease is None or self.consumed:
            return
        board_id, gateway_id, task_id = self._ids
        try:  # never consumed = nothing started locally: the Board keeps the card first-start (review F2)
            _client().release(board_id=board_id, task_uid=task_id, run_uid=self.lease.run_uid, gateway_id=gateway_id,
                              unused_grant=True)
        except _CLIENT_ERRORS as err:  # the lease then simply expires on the server
            _log.info("kanban sync: release of unused lease for %s failed: %s", task_id, err)


ORIGIN_LOCAL = "local"  # this host refused: its admission gate (kanban_admission), placement or worker capacity
ORIGIN_BOARD = "board"  # the Board service refused the lease, or could not be reached


class ClaimDeferral(str):
    """A claim deferral reason that knows who set it (``origin``: :data:`ORIGIN_LOCAL` / :data:`ORIGIN_BOARD`).
    A ``str``, so every consumer that compares, logs or serialises the reason is unchanged; the dispatch skip
    words read ``origin`` instead of guessing from the reason's name (PR #2003 review). [daveey]"""

    origin: str

    def __new__(cls, reason: str, origin: str) -> ClaimDeferral:
        self = super().__new__(cls, reason)
        self.origin = origin
        return self


# task_id -> why the claim was not granted (a :class:`ClaimDeferral`), set by :func:`claim_gate` and this host's
# admission gate and read once by the dispatcher (:func:`pop_claim_deferral`), so a tick can tell a deferred claim
# from a lost CAS and the "dispatcher stuck" warning can name it.
_CLAIM_DEFERRALS: dict[str, ClaimDeferral] = {}


def defer_claim(task_id: str, reason: str, origin: str) -> None:
    _CLAIM_DEFERRALS[task_id] = ClaimDeferral(reason, origin)


def pop_claim_deferral(task_id: str) -> Optional[ClaimDeferral]:
    return _CLAIM_DEFERRALS.pop(task_id, None)


def _source_skip(conn: sqlite3.Connection, row: sqlite3.Row) -> Optional[str]:
    """A source card this host already refused at its current manifest revision (host scope, v3 §6b skip memo)."""
    from hermes_cli import kanban_workspace_source as kws
    snap = kws.replica_snapshot(row)
    if snap is None or snap.manifest_value is None:
        return None
    try:
        revision = kws.parse_manifest(snap.manifest_value).manifest_revision
    except kws.ManifestInvalid:
        return None
    code = kws.skipped(conn, row["id"], revision)
    return f"workspace_source_refused_here:{code}" if code else None


def claim_gate(conn: sqlite3.Connection, task_id: str, expires: int, lane: str = "ready") -> ClaimGate:
    """Lease seam before a claim CAS. Only ``primary`` boards ask the service;
    without a grant the card stays ``ready`` (no offline claims: every card is
    at-most-once in v1). A grant bounds the local claim to the lease deadline."""
    from hermes_cli.kanban_worker_placement import execution_refusal
    from hermes_cli.kanban_sync_diagnostics import record_claim
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    from hermes_cli.kanban_requeue_custody import claim_refusal as custody_refusal
    if row is not None and (reason := execution_refusal(conn, row) or _source_skip(conn, row)
                            or custody_refusal(conn, row)):  # a requeued run's processes still live elsewhere
        defer_claim(task_id, reason, ORIGIN_LOCAL)
        return ClaimGate(expires, deferred=True)
    if board_mode(conn) != "primary":
        return ClaimGate(expires)
    from hermes_cli.kanban_sync_claims import DEFERRAL, held
    if held(conn):  # this host's first sync round has not reached the feed head (t_01a1d6c4e700958e)
        defer_claim(task_id, DEFERRAL, ORIGIN_LOCAL)
        return ClaimGate(expires, deferred=True)
    state = _state(conn)
    ids = (state["board_id"], state["gateway_id"], task_id)
    run_uid = uuid7()
    try:
        from hermes_cli.kanban_source_board import grant_protocol
        protocol = grant_protocol()  # workspace_source: only the dispatcher's claim, only when enabled (R3-a)
        lease = _client().acquire(board_id=ids[0], task_uid=task_id, run_uid=run_uid, gateway_id=ids[1],
                                  ttl_s=max(expires - int(time.time()), 1), lane=lane,
                                  **({"source_protocol": protocol} if protocol else {}))
    except _CLIENT_ERRORS as exc:  # fail closed: an unreachable service defers the claim
        _log.info("claim_deferred: offline task=%s (%s: %s)", task_id, type(exc).__name__, exc)
        defer_claim(task_id, "offline", ORIGIN_BOARD)
        record_claim(conn, task_id, "offline")
        return ClaimGate(expires, deferred=True)
    if not lease.granted:
        _log.info("claim_deferred: %s task=%s holder=%s", lease.reason or "denied", task_id, lease.holder)
        defer_claim(task_id, lease.reason or "denied", ORIGIN_BOARD)
        record_claim(conn, task_id, lease.reason or "denied")
        return ClaimGate(expires, deferred=True)
    record_claim(conn, task_id, "granted")
    deadline = lease.expires_ms // 1000 - LEASE_SAFETY_MARGIN_SECONDS
    gate = ClaimGate(min(expires, deadline), lease=lease, ids=ids)
    # Review ruling 2: the worker load formula guards a WORKER's claim of someone else's card. The origin claiming
    # its own card (hub overflow, review continuation) is gated by the measured-CPU/cap gate in claim_transaction
    # instead; normalized load is never a substitute for that measurement.
    if row is not None and row["placement_policy"] and row["created_on"] != ids[1]:
        from hermes_cli.kanban_worker_placement import local_admission
        if not local_admission(conn):
            with gate:
                pass  # release the unused grant; no local task/run was started
            defer_claim(task_id, "waiting_for_worker_capacity", ORIGIN_LOCAL)
            return ClaimGate(expires, deferred=True)
    return gate


def stage_claim(conn: sqlite3.Connection, task_id: str, gate: Optional[ClaimGate]) -> Optional[str]:
    """Inside the claim txn, after the CAS won: mirror the lease onto the task
    and return its run uid for the run row the caller inserts next.

    [daveey] The caller writes that uid into the run row itself. Handing it over through
    ``kanban_sync_state.pending_run_uid`` (consumed by the insert trigger) failed whenever the trigger was missing:
    the uid stayed pending, the Board's echo of the claim made a second run row with it, and the next local run
    insert, on any card, failed UNIQUE(task_runs.uid). A stale pending uid is cleared here as well."""
    if gate is None or gate.lease is None:
        return None
    lease = gate.lease
    conn.execute("UPDATE kanban_sync_state SET pending_run_uid = NULL WHERE pending_run_uid IS NOT NULL")
    conn.execute("UPDATE tasks SET lease_holder = ?, lease_expires = ?, "
                 "lease_version = COALESCE(?, lease_version) WHERE id = ?",
                 (gate._ids[1], lease.expires_ms, lease.version, task_id))
    from hermes_cli.kanban_workspace_source import stash_grant_snapshot
    stash_grant_snapshot(task_id, lease.source_snapshot)  # the resolver decides from the grant, not the replica
    gate.consumed = True
    return lease.run_uid


def _renewal_due(task_id: str, lease_expires_ms: int, ttl_s: int) -> bool:
    """At most one renewal attempt per ``ttl/3``, and only once a third of the lease is used.

    [daveey] A lease already inside the safety margin (lapsed while the process was suspended: the throttle is
    measured on the monotonic clock, which stops during macOS sleep, while the lease is wall-clock) is retried every
    ``LAPSED_RENEW_RETRY_SECONDS`` instead, so the first heartbeat after a wake renews it."""
    remaining_s = lease_expires_ms / 1000 - time.time()
    last = _RENEW_ATTEMPTS.get(task_id)
    spacing = LAPSED_RENEW_RETRY_SECONDS if remaining_s <= LEASE_SAFETY_MARGIN_SECONDS else ttl_s / 3
    if remaining_s > ttl_s * 2 / 3 or (last is not None and time.monotonic() - last < spacing):
        return False
    _RENEW_ATTEMPTS[task_id] = time.monotonic()
    return True


def heartbeat_expiry(conn: sqlite3.Connection, task_id: str, expires: int) -> int:
    """Heartbeat seam: renew the lease when due and bound the claim extension to
    the lease deadline. A worker that cannot renew therefore goes stale at
    ``lease_expires - margin`` and the dispatcher's stale-claim path stops it."""
    if board_mode(conn) != "primary":
        return expires
    row = conn.execute(
        "SELECT t.lease_expires, r.uid AS run_uid FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?", (task_id,),
    ).fetchone()
    if row is None or row["lease_expires"] is None:
        return expires
    lease_expires = row["lease_expires"]
    ttl_s = max(expires - int(time.time()), 1)
    # [daveey] No current run (it just ended continued/blocked): there is no run uid to renew under and the Board
    # refuses an empty one (400 "run_uid required"), so make no call; the bound below still applies.
    if row["run_uid"] and _renewal_due(task_id, lease_expires, ttl_s):
        lease_expires = _renew(conn, task_id, row["run_uid"], ttl_s, lease_expires) or lease_expires
    return min(expires, lease_expires // 1000 - LEASE_SAFETY_MARGIN_SECONDS)


def _renew(conn: sqlite3.Connection, task_id: str, run_uid: str, ttl_s: int,
           old_expires_ms: Optional[int] = None) -> Optional[int]:
    from hermes_cli.kanban_db_connect import write_txn
    state = _state(conn)
    try:
        lease = _client().renew(board_id=state["board_id"], task_uid=task_id, run_uid=run_uid,
                                gateway_id=state["gateway_id"], ttl_s=ttl_s)
    except _CLIENT_ERRORS as exc:
        _log.warning("kanban sync: lease renewal for %s failed (%s: %s)", task_id, type(exc).__name__, exc)
        return None
    with write_txn(conn):
        if lease.granted:
            conn.execute("UPDATE tasks SET lease_expires = ?, "
                         "lease_version = COALESCE(?, lease_version) WHERE id = ?",
                         (lease.expires_ms, lease.version, task_id))
            # [daveey] A renewal that rescues a lapsed lease (the first heartbeat after macOS sleep) is the only
            # evidence that path ran; log it like the session-turn lease does.
            left_s = None if old_expires_ms is None else old_expires_ms / 1000 - time.time()
            if left_s is not None and left_s <= LEASE_SAFETY_MARGIN_SECONDS:
                _log.info("kanban sync: lease of %s renewed %ds after it lapsed", task_id, max(0, int(-left_s)))
            return lease.expires_ms
        _log.warning("kanban sync: lease for %s lost to %s (%s)", task_id, lease.holder, lease.reason)
        conn.execute("UPDATE tasks SET lease_holder = ? WHERE id = ?", (lease.holder, task_id))
    return None


def lease_lost(conn: sqlite3.Connection, task_id: str, now: int, reassert_ttl_s: Optional[int] = None) -> bool:
    """True when this gateway no longer holds the card's lease, or the lease is
    within the safety margin of expiring (renewal kept failing).

    [daveey] With ``reassert_ttl_s`` (the caller is about to stop a live worker), a lapse on our clock is checked
    with the Board first (:func:`reassert_lease`): only ``lost`` counts, so a Mac that slept keeps its workers."""
    if board_mode(conn) != "primary":
        return False
    row = conn.execute("SELECT lease_holder, lease_expires FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["lease_expires"] is None:
        return False
    own = _state(conn)["gateway_id"]
    lost = row["lease_holder"] != own or now >= row["lease_expires"] // 1000 - LEASE_SAFETY_MARGIN_SECONDS
    return lost and (reassert_ttl_s is None or reassert_lease(conn, task_id, now, reassert_ttl_s) == "lost")


def reassert_lease(conn: sqlite3.Connection, task_id: str, now: int, ttl_s: int) -> str:
    """[daveey] Before a live local worker is stopped for :func:`lease_lost`, ask the Board.

    A lease past its expiry on our clock is not proof the Board gave the card away: the machine may have slept
    through the renewals (its workers frozen with it) and the Board holds a dark holder's lease (DARK_HOLD_MAX_MS).
    Returns ``held`` (renewed: keep the worker), ``unknown`` (Board unreachable and the lapse is within
    ``LEASE_DARK_HOLD_SECONDS``: keep it and ask again) or ``lost`` (another holder, a lease the Board already
    ended, a refused renewal, or offline past the hold: stop it)."""
    row = conn.execute(
        "SELECT t.lease_holder, t.lease_expires, r.uid AS run_uid FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?", (task_id,),
    ).fetchone()
    if row is None or row["lease_expires"] is None or row["run_uid"] is None:
        return "lost"
    if row["lease_holder"] != _state(conn)["gateway_id"]:
        return "lost"  # the Board's feed moved or ended the lease: a real takeover, not a gap on our side
    try:
        lease = _client().renew(board_id=_state(conn)["board_id"], task_uid=task_id, run_uid=row["run_uid"],
                                gateway_id=_state(conn)["gateway_id"], ttl_s=max(int(ttl_s), 1))
    except _CLIENT_ERRORS as exc:
        lapsed_s = now - int(row["lease_expires"]) // 1000
        _log.info("kanban sync: lease of %s lapsed %ds ago and the Board is unreachable (%s: %s)",
                  task_id, lapsed_s, type(exc).__name__, exc)
        return "unknown" if lapsed_s <= LEASE_DARK_HOLD_SECONDS else "lost"
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        if lease.granted:
            conn.execute("UPDATE tasks SET lease_expires = ?, lease_version = COALESCE(?, lease_version) "
                         "WHERE id = ?", (lease.expires_ms, lease.version, task_id))
            _log.info("kanban sync: lease of %s re-asserted after a renewal gap", task_id)
            return "held"
        _log.warning("kanban sync: lease for %s lost to %s (%s)", task_id, lease.holder, lease.reason)
        conn.execute("UPDATE tasks SET lease_holder = ? WHERE id = ?", (lease.holder, task_id))
    return "lost"


def release_lease(conn: sqlite3.Connection, task_id: str, run_uid: Optional[str]) -> None:
    """Best-effort release after an operator reclaim (post-commit)."""
    if board_mode(conn) != "primary" or run_uid is None:
        return
    state = _state(conn)
    try:
        _client().release(board_id=state["board_id"], task_uid=task_id, run_uid=run_uid,
                          gateway_id=state["gateway_id"])
    except _CLIENT_ERRORS as exc:
        _log.info("kanban sync: lease release for %s failed (%s); it expires on the server", task_id, exc)


def held_by_us_live(conn: sqlite3.Connection, task_id: str, now: Optional[int] = None) -> bool:
    """True when the mirrored Board lease on ``task_id`` is this gateway's and has not expired.

    The Board's lease grant writes the card ``running`` and the feed can apply that row before (or,
    when the claim is then refused and its lease released, instead of) the local claim CAS, so the
    card reads ``running`` with no ``claim_lock`` while the claim is still being published. False on
    a board that is not primary (no lease mirror)."""
    if board_mode(conn) != "primary":
        return False
    row = conn.execute("SELECT lease_holder, lease_expires FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["lease_holder"] is None or row["lease_expires"] is None:
        return False
    now_ms = int((time.time() if now is None else now) * 1000)
    return row["lease_holder"] == _state(conn)["gateway_id"] and int(row["lease_expires"]) > now_ms


def foreign_live_lease(conn: sqlite3.Connection, task_id: str, now: Optional[float] = None) -> Optional[str]:
    """Gateway id of an unexpired Board lease held by someone else, else None.

    A primary board mirrors cards running on another machine (``status='running'``,
    ``claim_lock`` NULL, ``lease_holder`` = them). Operator reclaim on that replica
    used to close the run row; the close synced and the owner's terminal-worker
    reaper SIGTERMed the live worker (David-MBP 2026-10-28 23:05, pid 1522 drain
    window: hub workers 631696/762267 died ~3 min after a Mac reclaim the Board
    refused). Empty when this gateway holds the lease, the lease has expired, or
    the board does not sync.
    """
    if board_mode(conn) != "primary":
        return None
    row = conn.execute(
        "SELECT lease_holder, lease_expires FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None or row["lease_holder"] is None or row["lease_expires"] is None:
        return None
    if row["lease_holder"] == _state(conn)["gateway_id"]:
        return None
    now_ms = int((time.time() if now is None else now) * 1000)
    if int(row["lease_expires"]) <= now_ms:
        return None
    return str(row["lease_holder"])


def refuse_foreign_reclaim(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when reclaim must not close the run: another gateway still holds the lease.

    Writes ``reclaim_refused`` and leaves the run open. Closing it here syncs an
    ended run to the lease owner, whose terminal-worker reaper then SIGTERMs the
    live process.
    """
    holder = foreign_live_lease(conn, task_id)
    if not holder:
        return False
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli import kanban_db as _kb
    with write_txn(conn):
        _kb._append_event(conn, task_id, "reclaim_refused", {
            "reason": "foreign_live_lease",
            "lease_holder": holder,
            "manual": True,
        })
    return True


def held_elsewhere_sql(conn: sqlite3.Connection, alias: str = "") -> str:
    """SQL condition (``AND NOT ...``) that leaves out cards whose Board lease another gateway holds.

    On a primary board a card this gateway created can run on another machine (Placement): its status is
    ``running`` here with no local claim. The dispatcher's sweeps (orphans, stale heartbeats) and its concurrency
    counts must not see it, or they would requeue a card that is running elsewhere, or spend a local slot on it.
    Empty on a board that is not primary."""
    if board_mode(conn) != "primary":
        return ""
    own = str(_state(conn)["gateway_id"]).replace("'", "''")
    a = f"{alias}." if alias else ""
    return f" AND NOT ({a}lease_holder IS NOT NULL AND {a}lease_holder != '{own}' AND {a}claim_lock IS NULL)"


def set_runs_on(conn: sqlite3.Connection, task_id: str, runs_on: Any) -> None:
    """``create_task(runs_on=...)``: validate the labels and write them (captured and pushed like any field).
    None is the default placement and writes nothing; a board that does not sync cannot carry them."""
    if runs_on is None:
        return
    from hermes_cli import kanban_worker_node as wn
    wn.set_runs_on(conn, task_id, wn.normalize_runs_on(runs_on if isinstance(runs_on, str) else list(runs_on)))


def worker_pickup(conn: sqlite3.Connection, budget: Any, dry_run: bool, profile_exists_fn: Any) -> frozenset:
    """Copy eligible foreign cards before the dispatcher freezes its lane candidates and screens admission.
    Retain the deferred-id return contract for dispatcher callers."""
    if dry_run or board_mode(conn) != "primary":
        return frozenset()
    from hermes_cli import kanban_worker_node as wn
    try:
        wn.pickup(conn, budget=budget, profile_exists=profile_exists_fn())
    except Exception:  # health: allow BLE001 -- a failed pickup must not stop the tick dispatching local work
        _log.warning("kanban: worker-node pickup failed", exc_info=True)
    return frozenset()  # admission runs after the dispatcher freezes its claim candidates


def worker_pickup_capped(conn: sqlite3.Connection, result: Any, dry_run: bool, profile_exists_fn: Any) -> Any:
    """Dispatcher seam for a tick with no spawn slot: pickup does not run, so record why the cards pinned to this
    machine wait (``budget_exhausted`` for the ones a slot would start; ``kanban_pickup_stall``). Returns ``result``."""
    if dry_run or board_mode(conn) != "primary":
        return result
    from hermes_cli import kanban_worker_node as wn
    try:
        wn.note_capped(conn, profile_exists_fn())
    except Exception:  # health: allow BLE001 -- pickup evidence must not stop the tick
        _log.warning("kanban: worker-node capped pickup note failed", exc_info=True)
    return result


def run_uid(conn: sqlite3.Connection, run_id: Optional[int]) -> Optional[str]:
    if run_id is None or board_mode(conn) == "off":
        return None
    row = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    return row[0] if row else None


# --- Dispatcher wake ------------------------------------------------------------------------------

def wake_dispatcher() -> None:
    _DISPATCHER_WAKE.set()


def consume_dispatcher_wake() -> bool:
    """True once after :func:`apply_remote_changes` made work claimable."""
    if not _DISPATCHER_WAKE.is_set():
        return False
    _DISPATCHER_WAKE.clear()
    return True


# --- Apply path -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteChange:
    seq: int
    table: str
    uid: str
    op: str  # 'upsert' | 'delete' | 'lease'
    fields: Mapping[str, Any]
    version: int
    hlc: str
    origin_gateway: str
    client_op_id: Optional[str] = None


@dataclass
class ApplyResult:
    cursor: int
    applied: int = 0
    skipped_echo: int = 0
    skipped: int = 0
    hooks: list[tuple[str, str, dict]] = field(default_factory=list)
    changed_fields: dict[str, set[str]] = field(default_factory=dict)
    wake_dispatcher: bool = False
    blobs_to_fetch: list[str] = field(default_factory=list)
    # [daveey] Cards this gateway owns that received a worker's ``decision_requested`` (kanban_decision_ask).
    decision_asks: set[str] = field(default_factory=set)
    # [daveey] A feed pull or snapshot: task fields with an unanswered local edit keep the local value
    # (``_without_unacked_fields``), checked inside the apply transaction.
    local_pending_wins: bool = False
    # [daveey] ``kanban_repair_revision`` read right after the apply transaction began and right before it
    # committed (None: no table or row). The sync client credits a live revision change made only by applies.
    revision_before: Optional[int] = None
    revision_after: Optional[int] = None
    # [daveey] Cards this page moved out of done/archived, and cards it brought a reopen event for
    # (``kanban_terminal_guard.enforce_after_apply``, run at the end of the page in the same transaction).
    left_terminal: list[tuple[str, str]] = field(default_factory=list)
    reopened: set[str] = field(default_factory=set)
    # [daveey] Every card whose status this page changed, with the new status (a shadow copy too: it fires no
    # observers, but the sync client re-probes the parks that name these cards, ``kanban_awaited_card_wake``).
    status_changed: dict[str, str] = field(default_factory=dict)


def _repair_revision(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("SELECT revision FROM kanban_repair_revision WHERE id = 1").fetchone()
    except sqlite3.OperationalError:  # no such table: a board the sync client never repaired
        return None
    return None if row is None else row[0]


_STATUS_HOOKS = {"running": "kanban_task_claimed", "done": "kanban_task_completed", "blocked": "kanban_task_blocked"}
_CLAIMABLE = ("ready", "review")


def apply_remote_changes(
    conn: sqlite3.Connection, changes: Sequence[RemoteChange], *, new_cursor: int, board: Optional[str] = None,
    observers: bool = True, local_pending_wins: bool = False,
    keep: Optional[Callable[[sqlite3.Connection, RemoteChange], bool]] = None,
) -> ApplyResult:
    """Apply one feed page in ONE transaction, then fire local observers.

    The ``applying`` flag suppresses the capture triggers (no echo), our own op
    coming back (``client_op_id`` still in the outbox) only clears that outbox
    row, ``run_uid`` is translated to the local run id, local-only columns are
    never written from the wire, and the cursor advances in the same commit as
    the rows. Hooks fire once, after the commit, with ``remote=True`` and
    ``origin_gateway``; a page that makes work claimable wakes the dispatcher.
    ``observers=False`` (a shadow copy) applies the rows and fires nothing. ``local_pending_wins`` (the feed
    pull and the snapshot rebuild) keeps every task field that has an unanswered local edit in the outbox, read
    in the same transaction as the apply. ``keep`` (the live-file rebuild sweep) is asked about each change inside
    that transaction; a change it refuses is skipped. [daveey]"""
    from hermes_cli.kanban_db_connect import write_txn
    result = ApplyResult(cursor=new_cursor, local_pending_wins=local_pending_wins)
    from hermes_cli import kanban_containment_replica as containment
    containment.prepare_apply(conn, changes)
    with write_txn(conn):
        result.revision_before = _repair_revision(conn)
        # Serialize with set_mode(off): a feed fetched before detach must not
        # overwrite locally held custody after the detach transaction commits.
        if board_mode(conn) == "off":
            raise RuntimeError("apply_remote_changes: kanban sync is not enabled on this board")
        if changes:
            conn.execute("UPDATE kanban_sync_state SET applying = 1")
            own = _state(conn)["gateway_id"]
            for change in changes:
                if keep is not None and not keep(conn, change):
                    result.skipped += 1
                    continue
                with containment.projection(conn, change):
                    _apply_one(conn, change, own, result)
            retained = containment.restore_page(conn, changes)
            result.hooks = [hook for hook in result.hooks if hook[1] not in retained]
            if result.left_terminal or result.reopened:  # a wake never reopens a done card [daveey]
                from hermes_cli.kanban_terminal_guard import enforce_after_apply
                enforce_after_apply(conn, result.left_terminal, result.reopened)
        # Cursor-only pages have no capture triggers to suppress. Avoid dirtying
        # the state row at all when both persisted values already match.
        conn.execute("UPDATE kanban_sync_state SET cursor = ?, applying = 0 "
                     "WHERE cursor IS NOT ? OR applying IS NOT 0", (new_cursor, new_cursor))
        result.revision_after = _repair_revision(conn)
    if observers:
        _after_commit(conn, result, board)
        from hermes_cli.kanban_source_refusal_block import drain
        drain(conn)
    return result


def _apply_one(conn: sqlite3.Connection, ch: RemoteChange, own: str, result: ApplyResult) -> None:
    if ch.table not in SYNCED_COLUMNS:
        raise ValueError(f"apply_remote_changes: unknown table {ch.table!r}")
    if ch.client_op_id:
        from hermes_cli.kanban_sync_omissions import acknowledge
        acknowledge(conn, ch.client_op_id, ch.fields)
    if ch.client_op_id and conn.execute(
            "DELETE FROM kanban_sync_outbox WHERE op_id = ?", (ch.client_op_id,)).rowcount:
        if ch.table == "tasks":
            conn.execute("UPDATE tasks SET sync_version = MAX(COALESCE(sync_version, 0), ?) WHERE id = ?",
                         (ch.version, ch.uid))
        result.skipped_echo += 1
        return
    handler = _APPLY_HANDLERS[(ch.table == "tasks", ch.op)]
    if handler(conn, ch, own, result):
        result.applied += 1
    else:
        result.skipped += 1


def _tombstoned(conn: sqlite3.Connection, table: str, uid: Any) -> bool:
    return conn.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name = ? AND row_uid = ?",
                        (table, uid)).fetchone() is not None


def _local_run_id(conn: sqlite3.Connection, uid: Optional[str], task_id: Any) -> Optional[int]:
    """Local integer id for a run uid; a run not seen yet gets a placeholder row
    that its own upsert fills in later."""
    if uid is None:
        return None
    row = conn.execute("SELECT id FROM task_runs WHERE uid = ?", (uid,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO task_runs (uid, task_id, status, started_at) VALUES (?, ?, 'pending_sync', 0)", (uid, task_id),
    ).lastrowid


def _local_fields(conn: sqlite3.Connection, table: str, fields: Mapping[str, Any], task_id: Any) -> dict[str, Any]:
    """Wire fields -> local columns: drop local-only/key/unknown columns, uids -> local ids, ms -> seconds."""
    present = set(table_columns(conn, table))
    allowed = set(captured_columns(conn, table)) | (SERVER_COLUMNS.get(table, frozenset()) & present)
    out: dict[str, Any] = {}
    for column in allowed:
        wire = _wire_name(table, column)
        if wire not in fields:
            continue
        value = _from_wire(fields[wire])
        if column in SECONDS_COLUMNS.get(table, ()):
            value = to_seconds(value)
        if column in JSON_SERVER_COLUMNS.get(table, ()):
            from hermes_cli.kanban_workspace_source import json_column
            value = json_column(value)
        if table == "tasks" and column == "priority" and value is not None:
            from hermes_cli.kanban_priority import bounded
            value = bounded(value)
        out[column] = _local_run_id(conn, value, task_id) if (table, column) in REF_COLUMNS else value
    return out


def _upsert_task(conn: sqlite3.Connection, ch: RemoteChange, own: str, result: ApplyResult) -> bool:
    if _tombstoned(conn, "tasks", ch.uid):
        return False
    old = conn.execute("SELECT * FROM tasks WHERE id = ?", (ch.uid,)).fetchone()
    if old is not None and old["sync_version"] is not None and ch.version < old["sync_version"]:  # equal = the same row (a re-send or a revert)
        return False
    fields = _without_unacked_fields(conn, ch) if result.local_pending_wins else ch.fields
    from hermes_cli.kanban_sync_omissions import without_held
    cols = _local_fields(conn, "tasks", without_held(conn, "tasks", ch.uid, fields), ch.uid)
    if old is None:
        names = ["id", *cols, "sync_version"]
        conn.execute(f"INSERT INTO tasks ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                     (ch.uid, *cols.values(), ch.version))
        changed = set(cols)
        from hermes_cli.kanban_restore_chat_subs import note_restored_task
        note_restored_task(conn, ch.uid, cols.get("created_on"), own)  # our own card back from the Board
    else:
        changed = {c for c, v in cols.items() if old[c] != v}
        if "status" in changed and old["status"] in ("done", "archived") and cols["status"] not in ("done", "archived"):
            result.left_terminal.append((ch.uid, old["status"]))
        assignments = ", ".join(f"{c} = ?" for c in cols)
        conn.execute(f"UPDATE tasks SET {assignments}{', ' if cols else ''}sync_version = ? WHERE id = ?",
                     (*cols.values(), ch.version, ch.uid))
        from hermes_cli.kanban_release_claim import release_on_feed
        release_on_feed(conn, ch.uid, old, cols.get("status"))  # our queued failure release landed [daveey]
    if "lease_holder" in ch.fields:  # the service's full row carries its lease mirror
        _mirror_lease(conn, ch.uid, ch.fields.get("lease_holder"), ch.fields.get("lease_expires"),
                      ch.version, own, result)
    _collect_task_observers(ch, cols.get("status"), changed, result)
    return True


def _without_unacked_fields(conn: sqlite3.Connection, ch: RemoteChange) -> Mapping[str, Any]:
    """The feed's task fields minus those with a local edit the Board has not answered yet (an outbox op that is not
    rejected): the newer local value wins until that op's answer arrives, the rule ``without_pending`` applies on
    the pull and ``_local_row_is_newer`` on child rows. [daveey] Read inside the apply transaction. The pull's
    pre-filter reads the outbox before the apply takes the write lock, so a claim that committed in between was
    not in it, and the Board's pre-claim echo (an equal version, which the version fence lets through) set the
    card back to ``ready`` with no current run while its worker was being spawned (MacBook t_01a189343dd2f6ac run
    9821, 2026-10-29)."""
    pending: set[str] = set()
    for (fields,) in conn.execute("SELECT fields FROM kanban_sync_outbox WHERE table_name = 'tasks' AND row_uid = ? "
                                  "AND state != 'rejected' AND op IN ('upsert', 'import')", (ch.uid,)):
        try:
            pending.update(json.loads(fields or "{}"))
        except ValueError:
            continue
    if not pending:
        return ch.fields
    return {k: v for k, v in ch.fields.items() if k not in pending}


def _mirror_lease(conn: sqlite3.Connection, task_id: str, holder: Optional[str], expires: Optional[int],
                  version: int, own: str, result: ApplyResult) -> bool:
    """Mirror the service's lease onto the task. A lease that moved away from this gateway while a local
    worker holds the claim makes the claim stale now, so the dispatcher's stale-claim path stops the worker."""
    # RPC grants/renewals acknowledge only the lease, not the rest of the task.
    # An older full-row echo may still carry task fields we have never applied.
    row = conn.execute("SELECT status, claim_lock, lease_holder, lease_version FROM tasks WHERE id = ?",
                       (task_id,)).fetchone()
    if row is None or (row["lease_version"] is not None and version < row["lease_version"]):
        return False
    conn.execute("UPDATE tasks SET lease_holder = ?, lease_expires = ?, lease_version = ? WHERE id = ?",
                 (holder, expires, version, task_id))
    if holder != own and row["claim_lock"] is not None and row["lease_holder"] == own:
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ? AND claim_lock IS NOT NULL",
                     (int(time.time()) - 1, task_id))
    if holder is None and row["status"] in _CLAIMABLE:
        result.wake_dispatcher = True
    return True


def _collect_task_observers(ch: RemoteChange, new_status: Optional[str], changed: set[str],
                            result: ApplyResult) -> None:
    kwargs = {"remote": True, "origin_gateway": ch.origin_gateway}
    if "status" in changed and new_status:
        result.status_changed[ch.uid] = new_status
    if "status" in changed and new_status in _STATUS_HOOKS:
        result.hooks.append((_STATUS_HOOKS[new_status], ch.uid, kwargs))
    if "status" in changed and new_status in _CLAIMABLE:
        result.wake_dispatcher = True
    other = changed - {"status"}
    if other:
        result.changed_fields.setdefault(ch.uid, set()).update(other)


def _upsert_row(conn: sqlite3.Connection, ch: RemoteChange, own: str, result: ApplyResult) -> bool:
    table = ch.table
    task_id = ch.fields.get("task_id") or ch.fields.get("parent_id")
    if _tombstoned(conn, "tasks", task_id) or (table != "task_links" and _tombstoned(conn, table, ch.uid)):
        return False
    from hermes_cli.kanban_sync_omissions import without_held
    cols = _local_fields(conn, table, without_held(conn, table, ch.uid, ch.fields), task_id)
    if table == "task_events" and "origin_gateway" in table_columns(conn, table):
        # Authority, not provenance only: kanban_review_release.released() honours a promoted event's
        # review_release stamp only when this column names the card's home, so it must come from the wire. [daveey]
        cols["origin_gateway"] = ch.origin_gateway or None
    if table == "task_links":
        return conn.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                            (cols["parent_id"], cols["child_id"])).rowcount == 1
    existing = conn.execute(f"SELECT id FROM {table} WHERE uid = ?", (ch.uid,)).fetchone()
    if existing is not None:
        if _local_row_is_newer(conn, table, ch, existing[0]):
            return False
        if table == "task_runs" and ch.version:
            cols["sync_version"] = ch.version
        assignments = ", ".join(f"{c} = ?" for c in cols)
        if cols:
            conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*cols.values(), existing[0]))
        return True
    if table == "task_runs" and ch.version:
        cols["sync_version"] = ch.version
    if table == "task_attachments":
        cols["stored_path"] = ""
        result.blobs_to_fetch.append(ch.uid)
    if table == "task_comments" and "origin_gateway" in table_columns(conn, table):
        # Machine-local, so never copied from the wire as a column: derive it the way a local write does, from
        # the author (a relay names its sender) or else the gateway that wrote the row. [daveey]
        from hermes_cli.kanban_comment_origin import synced_origin
        origin = synced_origin(cols.get("author"), ch.origin_gateway)
        if origin:
            cols["origin_gateway"] = origin
    names = ["uid", *cols]
    conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                 (ch.uid, *cols.values()))
    if table == "task_events" and task_id:
        from hermes_cli.kanban_terminal_guard import is_reopen
        if is_reopen({"kind": cols.get("kind"), "payload": cols.get("payload")}):
            result.reopened.add(str(task_id))
    if table == "task_events" and task_id and cols.get("kind") == "decision_requested":
        row = conn.execute("SELECT created_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None and (not row[0] or row[0] == own):
            result.decision_asks.add(str(task_id))  # the owner checks it can deliver, after the commit
    if table == "task_events" and ch.fields.get("kind") in ("archive_requested", "capability_owner_archive_requested"):
        result.wake_dispatcher = True  # a stop request (kanban_remote_stop) or forwarded owner archive to apply
    if table == "task_chat_links":
        from hermes_cli.kanban_restore_chat_subs import restore_link_sub
        restore_link_sub(conn, cols, own)  # a restored card's hub delivery (kanban_notify_subs does not sync)
    if table == "task_events" and cols.get("kind") == "origin_intent_recorded":
        from hermes_cli.kanban_origin_intent_carry import restore_from_event
        restore_from_event(conn, cols, own)  # a restored courier card's origin intent (a machine-local table)
    return True


def _local_row_is_newer(conn: sqlite3.Connection, table: str, ch: RemoteChange, row_id: int) -> bool:
    """True when the feed's copy of an existing child row must not overwrite it.

    Child rows have no per-field clocks, so the last write wins wherever it lands. Two copies are older than what
    this gateway holds: one while a local edit of the row is still in the outbox (the push sends the row whole and
    makes it the Board's newest version), and one at or below the Board version this gateway last applied or had
    acknowledged (``task_runs.sync_version``). Without this, our own earlier push of a run (its outbox row already
    gone) or the Board's lease-grant copy, pulled after the run ended here, reopened it as ``running`` and the next
    push sent that back to the Board."""
    if table == "task_runs" and ch.fields.get("ended_at") is not None and board_mode(conn) == "primary":
        local = conn.execute("SELECT ended_at, sync_version FROM task_runs WHERE id = ?", (row_id,)).fetchone()
        if local["ended_at"] is None and ch.version > (local["sync_version"] or 0):
            # A Board lease can expire before the local run's initial insert is
            # pushed. That pending startup is not newer than an authoritative
            # end. The push reserializes the now-ended row, retaining its end.
            return False
    if conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = ? AND row_uid = ? AND state = 'pending' "
                    "AND op != 'restore' LIMIT 1", (table, ch.uid)).fetchone():
        return True
    if table != "task_runs" or not ch.version:
        return False
    seen = conn.execute("SELECT sync_version FROM task_runs WHERE id = ?", (row_id,)).fetchone()[0]
    return seen is not None and ch.version <= seen


def _delete_task(conn: sqlite3.Connection, ch: RemoteChange, own: str, result: ApplyResult) -> bool:
    from hermes_cli import kanban_db as _kb
    _tombstone(conn, "tasks", ch.uid)
    if conn.execute("DELETE FROM tasks WHERE id = ?", (ch.uid,)).rowcount != 1:
        return False
    _kb._delete_task_relations(conn, ch.uid)
    result.wake_dispatcher = True  # a deleted parent satisfies its children
    return True


def _delete_row(conn: sqlite3.Connection, ch: RemoteChange, own: str, result: ApplyResult) -> bool:
    _tombstone(conn, ch.table, ch.uid)
    if ch.table == "task_links":
        parent, _, child = ch.uid.partition(">")
        return conn.execute("DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
                            (parent, child)).rowcount == 1
    return conn.execute(f"DELETE FROM {ch.table} WHERE uid = ?", (ch.uid,)).rowcount == 1


def _tombstone(conn: sqlite3.Connection, table: str, uid: str) -> None:
    conn.execute("INSERT OR REPLACE INTO kanban_sync_tombstones (table_name, row_uid, deleted_at) VALUES (?, ?, ?)",
                 (table, uid, time.time_ns() // 1_000_000))


def _lease_task(conn: sqlite3.Connection, ch: RemoteChange, own: str, result: ApplyResult) -> bool:
    """Lease-only changes share the lease fence and never acknowledge task fields."""
    return _mirror_lease(conn, ch.uid, ch.fields.get("lease_holder"), ch.fields.get("lease_expires"),
                         ch.version, own, result)


_APPLY_HANDLERS = {
    (True, "upsert"): _upsert_task,
    (False, "upsert"): _upsert_row,
    (True, "delete"): _delete_task,
    (False, "delete"): _delete_row,
    (True, "lease"): _lease_task,
}


def _after_commit(conn: sqlite3.Connection, result: ApplyResult, board: Optional[str]) -> None:
    from hermes_cli import kanban_db as _kb
    board = board or _kb.get_current_board()
    for event, task_id, kwargs in result.hooks:
        row = conn.execute("SELECT assignee, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        _kb._fire_kanban_lifecycle_hook(
            event, task_id, board=board, assignee=row["assignee"] if row else None,
            run_id=row["current_run_id"] if row else None, **kwargs,
        )
    if result.changed_fields and _kb._kanban_observer_consumed("on_kanban_task_updated"):
        for task_id, fields in result.changed_fields.items():
            _kb._fire_kanban_lifecycle_hook(
                "on_kanban_task_updated", task_id, board=board, changed_fields=sorted(fields), remote=True,
            )
    if result.decision_asks:
        from hermes_cli import kanban_decision_ask
        for task_id in sorted(result.decision_asks):
            try:
                kanban_decision_ask.owner_fallback(conn, task_id)
            except Exception as exc:  # health: allow BLE001 -- post-commit hook: one card's fallback never skips the rest of _after_commit (logged with traceback)
                _log.warning("kanban sync: decision ask fallback for %s failed: %s", task_id, exc, exc_info=True)
    if result.wake_dispatcher:
        wake_dispatcher()


def outbox(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Pending outbox rows in FIFO order, ``fields`` decoded (drain + inspection)."""
    rows = conn.execute("SELECT * FROM kanban_sync_outbox ORDER BY created_at, rowid").fetchall()
    return [{**dict(r), "fields": json.loads(r["fields"])} for r in rows]
