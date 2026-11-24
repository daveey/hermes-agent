"""Automatic recovery of a Kanban board whose table b-trees are corrupt. [daveey]

The connect-time guard (:mod:`hermes_cli.kanban_db_connect`) repairs index-only damage with
REINDEX and fails closed on everything else, so a board whose *table* pages are damaged stays
down until an agent notices and rebuilds it by hand. Done by hand, that rebuild went wrong on
the hub on 2026-10-28: three agents each built a salvage and ``mv``'d it over ``kanban.db``
while the gateway and Desktop backends still had WAL connections open, and every swap stranded
those connections and corrupted the next copy. This module is the supported path:

1. **Out of process.** :func:`spawn_recovery` runs ``python -m hermes_cli.kanban_db_recover``.
   The recovery reads and copies the database file, and a raw read in a process that has a
   connection to it cancels that process's POSIX locks (see ``sqlite_safe_read``). A child that
   opened nothing has nothing to lose. The child also re-checks the file from a clean process:
   a caller stranded on deleted ``-wal``/``-shm`` files sees "corruption" that is not on disk,
   and the child then reports ``healthy`` instead of rebuilding a good board.
2. **One recovery at a time.** ``<db>.recovery.lock`` (non-blocking flock; the same lock the
   sqlite-live-guard mod's swap_db.py takes). A second recovery, automatic or ``hermes kanban
   recover``, or a manual swap, exits ``busy`` instead of racing the first.
3. **Snapshot through SQLite.** The damaged board is copied with the backup API (consistent with
   its WAL), kept as ``<db>.recover-<ts>.damaged.bak``, and salvaged from that copy: every table
   is read forward and backward by rowid and through each of its indexes, so only rows on pages
   no path reaches are lost. Rows still missing are filled from the newest quarantine backup
   that passes ``quick_check`` (never outbox/state rows, never tombstoned uids).
4. **Loss accounting.** Per table: rows read, rows filled from the backup, and the keys an index
   still lists whose row could not be read (known lost). Written to
   ``<db>.recovery-<ts>.json`` and logged.
5. **Installed in place.** The rebuilt file is written into ``kanban.db`` with the backup API,
   under SQLite's own locking, so connections other processes hold stay valid and see the new
   content. The file is never renamed or unlinked.
6. **Follow-up.** A card on the recovered board carries the loss report, and boards attached to
   the sync service are reconciled with it (:func:`kanban_sync_client.reconcile`).
"""

from __future__ import annotations

# An entry point (``python -m hermes_cli.kanban_db_recover``): the bootstrap activates the
# dependency environment first. A no-op when imported by an already-bootstrapped process.
try:
    import hermes_bootstrap
except ModuleNotFoundError as exc:  # a partial ``hermes update`` can leave the bootstrap unregistered
    if exc.name != "hermes_bootstrap":
        raise

import argparse
import contextlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

_log = logging.getLogger(__name__)

# Rows of these tables are never filled from an older backup: a sent outbox op filled back in would
# be re-sent with stale fields, and sync state/cursors must reflect the live file only.
_NO_FILL_TABLES = frozenset({"kanban_sync_outbox", "kanban_sync_state", "sqlite_sequence",
                             "kanban_creator_empty_events",
                             # Indexed keys attest identity, not the latest custody,
                             # capture origin, command receipt or observation epoch.
                             # Preserve readable current rows; report lost rows rather
                             # than resurrect mutable protocol history from a backup.
                             "kanban_replay_deferred1", "kanban_replay_observation1",
                             "kanban_replay_observation_reads1", "kanban_replay_episode1",
                             "kanban_replay_diagnostic1", "kanban_replay_transport_fault1",
                             "kanban_failure_commands", "kanban_actionability_commands",
                             "kanban_sync_omissions", "kanban_sync_status_origins",
                             "kanban_sync_key_supersessions", "kanban_sync_key_supersession_ops",
                             "kanban_sync_key_supersession_children"})
# Certificates are local/disposable: preserve DDL but never salvage their data.
_DISPOSABLE_TABLES = frozenset({"kanban_creator_empty_events"})
# Keys listed per table in the report (the counts are always complete).
_REPORT_KEY_CAP = 200
# How long a spawned recovery may run before the parent stops waiting for it.
RECOVERY_TIMEOUT_SECONDS = 600
# Re-salvage when the live file changed while we were salvaging, at most this many times.
_MAX_SALVAGE_PASSES = 3


@dataclass
class TableLoss:
    """What happened to one table. Rows an index still lists but no read path (nor the backup
    fill) produced are lost: ``known_lost`` counts those listed by EVERY index of the table (a
    lower bound, robust to one damaged index listing stale rows), ``possibly_lost`` those listed
    by ANY index (an upper bound). ``unreadable_paths`` counts read paths that stopped on an error."""

    recovered: int = 0
    filled_from_backup: int = 0
    known_lost: int = 0
    possibly_lost: int = 0
    lost_keys: list[Any] = field(default_factory=list)
    unreadable_paths: int = 0
    # Index entries whose rowid a point lookup cleanly found absent (no read error): stale entries
    # of deleted rows, so they are neither filled from the backup nor counted as lost.
    stale_index_entries: int = 0


@dataclass
class RecoveryResult:
    """``status``: ``recovered`` (rebuilt and installed), ``healthy`` (a clean process found no
    damage: nothing written), ``index_only`` (REINDEX is enough; left to the guard), ``busy``
    (another recovery holds the lock), ``missing`` (no file), ``contended`` (the board was written
    during every rebuild pass: nothing installed, retried later), ``stranded_holders`` (a process
    still maps deleted -wal/-shm files of the board, so its checkpoint could overwrite an install:
    nothing installed), ``installed_unverified`` (written, but the post-install check failed),
    ``failed`` (nothing installed)."""

    status: str
    db_path: str
    reason: str = ""
    messages: list[str] = field(default_factory=list)
    snapshot_path: Optional[str] = None
    fill_source: Optional[str] = None
    report_path: Optional[str] = None
    card_id: Optional[str] = None
    sync: Optional[dict] = None
    tables: dict[str, TableLoss] = field(default_factory=dict)
    lost_rows: int = 0
    concurrent_writes: bool = False
    stranded_pids: list[int] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["tables"] = {k: asdict(v) for k, v in self.tables.items()}
        return out


# --- locking ----------------------------------------------------------------------------------

def _lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".recovery.lock")  # shared with sqlite-live-guard swap_db.py


@contextlib.contextmanager
def recovery_lock(db_path: Path) -> Iterator[bool]:
    """Non-blocking exclusive lock for one board's recovery. Yields False when another process
    holds it. The lock file itself is never written (a Windows byte lock must stay on byte 0); the
    holder (pid, session, card, start time) goes to ``<db>.recover.pid`` as JSON for the ``busy``
    message, the sqlite-live-guard refusal and the self-heal incident dedupe, and is removed on
    release."""
    lock_path = _lock_path(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - Windows: msvcrt byte lock on byte 0
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                yield False
                return
        except OSError:
            yield False
            return
        record = {"pid": os.getpid(), "proc_start": _proc_start(os.getpid()), "started_at": time.time(),
                  "session_id": os.environ.get("HERMES_SESSION_ID") or None,
                  "task_id": os.environ.get(HOLDER_TASK_ENV) or os.environ.get("HERMES_KANBAN_TASK") or None}
        with contextlib.suppress(OSError):
            _pid_path(db_path).write_text(json.dumps(record) + "\n", encoding="utf-8")
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                _pid_path(db_path).unlink()
    finally:
        handle.close()


def _pid_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".recover.pid")


# The card a recovery runs for. spawn_recovery strips HERMES_KANBAN_TASK from its child (the child
# is not that card's worker), so the caller's card travels in this variable instead.
HOLDER_TASK_ENV = "HERMES_RECOVERY_HOLDER_TASK"


def _pid_alive(pid: int) -> bool:
    from gateway.status import _pid_exists

    return _pid_exists(pid)


def recovery_lock_holder_info(db_path: Path) -> Optional[dict]:
    """``{pid, session_id, task_id, started_at}`` of the live process holding this board's
    recovery lock, or None (no record, or its process is gone). An old plain-pid record reads as
    ``{pid}``."""
    try:
        raw = _pid_path(db_path).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        info = {"pid": raw}
    if not isinstance(info, dict):
        info = {"pid": info}
    try:
        info["pid"] = int(info.get("pid"))
    except (TypeError, ValueError):
        return None
    if not _pid_alive(info["pid"]):
        return None
    if info.get("proc_start") and _proc_start(info["pid"]) not in (None, str(info["proc_start"])):
        return None  # the recorded process died (e.g. SIGKILL at the spawn timeout) and its pid was reused
    return info


def _proc_start(pid: int) -> Optional[str]:
    """Linux process start time in clock ticks (``/proc/<pid>/stat`` field 22); None elsewhere."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8-sig").rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def describe_holder(info: Optional[dict]) -> str:
    if not info:
        return "holder unknown"
    parts = [f"pid {info['pid']}"]
    if info.get("session_id"):
        parts.append(f"session {info['session_id']}")
    if info.get("task_id"):
        parts.append(f"card {info['task_id']}")
    return ", ".join(parts)


def recovery_lock_holder(db_path: Path) -> Optional[str]:
    info = recovery_lock_holder_info(db_path)
    return describe_holder(info) if info else None


# --- probing ----------------------------------------------------------------------------------

def _integrity(conn: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]


def classify(path: Path) -> tuple[str, list[str]]:
    """``(state, messages)`` for the file as THIS process sees it: ``ok``, ``index_only`` or
    ``corrupt``. Call from a process with no other connection to ``path``."""
    from hermes_cli.kanban_db_connect import _repairable_index_names
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            messages = _integrity(conn)
        finally:
            conn.close()
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as exc:
        return "corrupt", [f"sqlite refused to open file: {exc}"]
    if len(messages) == 1 and messages[0].strip().lower() == "ok":
        return "ok", messages
    if _repairable_index_names(messages):
        return "index_only", messages
    return "corrupt", messages


# --- salvage ----------------------------------------------------------------------------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema(conn: sqlite3.Connection) -> list[tuple[str, str, str, Optional[str]]]:
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    return [(str(t), str(n), str(tb), s) for t, n, tb, s in rows]


def _is_without_rowid(sql: Optional[str]) -> bool:
    return bool(sql and re.search(r"\)\s*WITHOUT\s+ROWID\s*;?\s*$", sql, re.IGNORECASE))


def _read_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> tuple[list[tuple], bool]:
    """Rows of ``sql`` up to the first read error; ``(rows, stopped_on_error)``."""
    rows: list[tuple] = []
    try:
        cur = conn.execute(sql, params)
        while True:
            try:
                row = cur.fetchone()
            except sqlite3.DatabaseError:
                return rows, True
            if row is None:
                return rows, False
            rows.append(tuple(row))
    except sqlite3.DatabaseError:
        return rows, True


def _index_columns(conn: sqlite3.Connection, index: str) -> list[str]:
    try:
        return [str(r[2]) for r in conn.execute(f"PRAGMA index_info({_q(index)})") if r[2] is not None]
    except sqlite3.DatabaseError:
        return []


def _table_indexes(conn: sqlite3.Connection, table: str) -> list[tuple[str, bool]]:
    """``(index_name, unique)`` for every full index on ``table`` (auto-indexes included). Partial
    indexes are skipped: ``INDEXED BY`` can't scan them unconstrained, and they list only some rows."""
    try:
        return [(str(r[1]), bool(r[2])) for r in conn.execute(f"PRAGMA index_list({_q(table)})")
                if not (len(r) > 4 and r[4])]
    except sqlite3.DatabaseError:
        return []


# index name -> (its key columns, {rowid it lists: key values})
Listed = dict[str, tuple[list[str], dict[int, tuple]]]


def _salvage_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, sql: Optional[str]
                   ) -> tuple[int, list[str], int, Listed, set[int]]:
    """Copy every readable row of ``table`` into ``dst``: ``(rows inserted, column names, failed read
    paths, listed, stale)``. ``listed`` maps each index to the rowids it still lists (with the index's
    key columns, for the report): a covering scan reads only the index b-tree, so it names rows whose
    table page may be unreadable. ``stale`` holds listed rowids a point lookup found cleanly absent (no
    read error): stale index entries of deleted rows, dropped from ``listed`` so they are never filled
    back in from a backup. The lookups cover every listed rowid ``dst`` lacks after the scans' insert,
    not those the scans missed: a damaged leaf can make a scan return a garbage row with any rowid,
    which the insert then drops (NOT NULL), and keying on scan output would let that hide a stale
    entry and make the backup fill resurrect the deleted row."""
    try:
        columns = [str(r[1]) for r in src.execute(f"PRAGMA table_info({_q(table)})")]
    except sqlite3.DatabaseError:
        columns = []
    if not columns:
        return 0, [], 1, {}, set()
    cols = ", ".join(_q(c) for c in columns)
    rowid = not _is_without_rowid(sql)
    rows: dict[Any, tuple] = {}
    failed = 0
    select = f"SELECT rowid, {cols} FROM {_q(table)}" if rowid else f"SELECT {cols} FROM {_q(table)}"
    paths = [select, select + (" ORDER BY rowid DESC" if rowid else " ORDER BY 1 DESC")]
    indexes = [(index, _index_columns(src, index)) for index, _unique in _table_indexes(src, table)]
    for index, icols in indexes:
        if icols:
            paths.append(f"{select} INDEXED BY {_q(index)} ORDER BY {', '.join(_q(c) for c in icols)}")
            paths.append(f"{select} INDEXED BY {_q(index)} ORDER BY "
                         f"{', '.join(_q(c) + ' DESC' for c in icols)}")
    for path in paths:
        got, stopped = _read_rows(src, path)
        failed += int(stopped)
        for row in got:
            if rowid:
                rows.setdefault(row[0], row[1:])
            else:
                rows.setdefault(row, row)
    names = ", ".join(_q(c) for c in columns)
    if rowid:
        insert = f"INSERT OR IGNORE INTO {_q(table)} (rowid, {names}) VALUES ({', '.join('?' * (len(columns) + 1))})"
        inserted = dst.executemany(insert, [(k, *v) for k, v in rows.items()]).rowcount
    else:
        insert = f"INSERT OR IGNORE INTO {_q(table)} ({names}) VALUES ({', '.join('?' * len(columns))})"
        inserted = dst.executemany(insert, list(rows.values())).rowcount
    inserted = max(inserted, 0)
    listed: Listed = {}
    stale: set[int] = set()
    if rowid:
        for index, icols in indexes:
            if not icols:
                continue
            order = ", ".join(_q(c) for c in icols)
            got, _ = _read_rows(src, f"SELECT rowid, {order} FROM {_q(table)} INDEXED BY {_q(index)} ORDER BY {order}")
            listed[index] = (icols, {int(r[0]): tuple(r[1:]) for r in got if isinstance(r[0], int)})
        # Point lookups for every listed row the insert did not produce: a scan that stops on a damaged
        # page can drop the row it was holding, and a garbage scan row can take a listed rowid and then
        # fail the insert, while a lookup reaches any intact leaf directly.
        have = {r[0] for r in dst.execute(f"SELECT rowid FROM {_q(table)}")}
        for rid in sorted({rid for _cols, entries in listed.values() for rid in entries} - have):
            got, stopped = _read_rows(src, f"{select} WHERE rowid = ?", (rid,))
            if not got and not stopped:
                stale.add(rid)
            found = [(rid, *row[1:]) for row in got if row[0] == rid][:1]
            if found:
                inserted += max(dst.executemany(insert, found).rowcount, 0)
        for _cols, entries in listed.values():
            for rid in stale:
                entries.pop(rid, None)
    return inserted, columns, failed, listed, stale


def _healthy_fill_source(db_path: Path, exclude: set[Path]) -> Optional[Path]:
    """The newest quarantine/pre-recover backup of this board that passes ``quick_check``."""
    parent, base = db_path.parent, db_path.name
    candidates = [p for p in parent.glob(f"{base}.*") if p.is_file() and p not in exclude
                  and (p.name.endswith(".bak") or ".pre-recover-" in p.name)
                  and not p.name.endswith((".damaged.bak", "-wal", "-shm"))]
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:  # rotated away while we list
            return -1.0
    candidates = [p for p in candidates if _mtime(p) >= 0]
    candidates.sort(key=_mtime, reverse=True)
    for cand in candidates[:12]:
        try:
            conn = sqlite3.connect(f"{cand.resolve().as_uri()}?immutable=1", uri=True)
            try:
                if conn.execute("PRAGMA quick_check").fetchone()[0] == "ok" and conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                    return cand
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            continue
    return None


def _tombstoned(dst: sqlite3.Connection) -> set[tuple[str, str]]:
    try:
        return {(str(t), str(u)) for t, u in dst.execute("SELECT table_name, row_uid FROM kanban_sync_tombstones")}
    except sqlite3.DatabaseError:
        return set()


def _build(snapshot: Path, out: Path, fill: Optional[Path], result: RecoveryResult) -> None:
    """Rebuild ``snapshot`` into a fresh ``out`` (WAL, same page size) and account per table."""
    src = sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
    src.execute("PRAGMA cell_size_check=ON")
    out.unlink(missing_ok=True)
    dst = sqlite3.connect(str(out), isolation_level=None)
    try:
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        user_version = src.execute("PRAGMA user_version").fetchone()[0]
        application_id = src.execute("PRAGMA application_id").fetchone()[0]
        dst.execute(f"PRAGMA page_size={int(page_size)}")
        dst.execute(f"PRAGMA user_version={int(user_version)}")
        dst.execute(f"PRAGMA application_id={int(application_id)}")
        schema = _schema(src)
        tables: list[tuple[str, Optional[str]]] = [
            (n, s) for t, n, _tb, s in schema if t == "table" and s and not n.startswith("sqlite_")]
        later = [s for t, n, _tb, s in schema if t in ("index", "trigger", "view") and s]
        has_seq = any(n == "sqlite_sequence" for t, n, _tb, _s in schema)
        dst.execute("BEGIN")
        for name, sql in tables:
            dst.execute(str(sql))
        listed_by_table: dict[str, Listed] = {}
        for name, sql in tables:
            if name in _DISPOSABLE_TABLES:
                result.tables[name] = TableLoss()
                continue
            inserted, _columns, failed, listed, stale = _salvage_table(src, dst, name, sql)
            listed_by_table[name] = listed
            loss = result.tables.setdefault(name, TableLoss())
            loss.unreadable_paths = failed
            loss.stale_index_entries = len(stale)
            loss.recovered = inserted
        if fill is not None:
            _fill_from_backup(dst, fill, tables, listed_by_table, result)
        # Lost: rowids an index still lists that neither the salvage nor the fill produced.
        for name, _sql in tables:
            listed = listed_by_table.get(name) or {}
            if not listed:
                continue
            have = {r[0] for r in dst.execute(f"SELECT rowid FROM {_q(name)}")}
            per_index = [set(entries) - have for _cols, entries in listed.values()]
            union = set().union(*per_index)
            both = set.intersection(*per_index) if per_index else set()
            keys: dict[int, tuple] = {}
            for _cols, entries in listed.values():
                for rid in union:
                    if rid in entries and rid not in keys:
                        keys[rid] = entries[rid]
            loss = result.tables[name]
            loss.known_lost, loss.possibly_lost = len(both), len(union)
            loss.lost_keys = [[rid, *keys.get(rid, ())] for rid in sorted(both or union)[:_REPORT_KEY_CAP]]
        if has_seq:
            floors = {name: max((rid for _cols, entries in (listed_by_table.get(name) or {}).values()
                                 for rid in entries), default=0) for name, _sql in tables}
            _rebuild_sequence(src, dst, tables, fill, floors)
        from hermes_cli.kanban_replay_admission import mark_rebuilt
        present = {name for name, _sql in tables}
        enrolled = 'kanban_replay_sender1' in present
        if not enrolled:
            try:
                row = src.execute('SELECT journal_id FROM kanban_sync_state LIMIT 1').fetchone()
            except sqlite3.DatabaseError:
                row = None
            enrolled = row is not None and row[0] is not None
        # A never-enrolled board has no replay custody to lose (replay tables exist only after
        # enrollment); damage to its legacy sync tables is healed by the ordinary legacy resync.
        prefixes = ('kanban_replay_', 'kanban_sync_', 'kanban_failure_', 'kanban_actionability_')
        mark_rebuilt(dst, {name: asdict(loss) for name, loss in result.tables.items()
                           if enrolled and name.startswith(prefixes) and
                           (loss.unreadable_paths or loss.known_lost or loss.possibly_lost)},
                     enrolled=enrolled)
        dst.execute("COMMIT")
        for sql in later:
            with contextlib.suppress(sqlite3.Error):  # an index the data now violates is reported by the check below
                dst.execute(sql)
        dst.commit()
        messages = _integrity(dst)
        if messages != ["ok"]:
            raise RuntimeError(f"rebuilt board fails integrity_check: {messages[:3]}")
        missing_objects = {n for t, n, _tb, s in schema if s and not n.startswith("sqlite_")} - {
            n for (n,) in dst.execute("SELECT name FROM sqlite_master")}
        if missing_objects:
            raise RuntimeError(f"rebuilt board is missing schema objects: {sorted(missing_objects)[:10]}")
    finally:
        dst.close()
        src.close()
    # A fresh connection: a statement a cursor above still holds would make the checkpoint fail
    # with "database table is locked".
    wal = sqlite3.connect(str(out), isolation_level=None)
    try:
        wal.execute("PRAGMA journal_mode=WAL").fetchall()
        wal.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        wal.close()
    result.lost_rows = sum(t.known_lost for t in result.tables.values())


def _fill_from_backup(dst: sqlite3.Connection, fill: Path, tables: list[tuple[str, Optional[str]]],
                      listed_by_table: dict[str, Listed], result: RecoveryResult) -> None:
    """Fill only rows the damaged file's own indexes still list but no read produced: a row the
    backup has and no index lists may have been deleted since the backup was taken, and filling it
    would resurrect it. A backup row qualifies when its rowid is listed AND, for every index that
    lists that rowid, its key columns equal the listed key (rowids alone can shift across a VACUUM)."""
    bak = sqlite3.connect(f"{fill.resolve().as_uri()}?immutable=1", uri=True)
    try:
        tomb = _tombstoned(dst)
        for name, sql in tables:
            if name in _NO_FILL_TABLES or _is_without_rowid(sql):
                continue
            listed = listed_by_table.get(name) or {}
            have = {r[0] for r in dst.execute(f"SELECT rowid FROM {_q(name)}")}
            wanted = {rid for _cols, entries in listed.values() for rid in entries} - have
            if not wanted:
                continue
            try:
                bcols = [str(r[1]) for r in bak.execute(f"PRAGMA table_info({_q(name)})")]
            except sqlite3.DatabaseError:
                continue
            dcols = [str(r[1]) for r in dst.execute(f"PRAGMA table_info({_q(name)})")]
            common = [c for c in dcols if c in bcols]
            if not common:
                continue
            uid_at = common.index("uid") if "uid" in common else None
            id_at = common.index("id") if "id" in common else None
            filled = 0
            for rid in sorted(wanted):
                got, _ = _read_rows(bak, f"SELECT rowid, {', '.join(_q(c) for c in common)} FROM {_q(name)} "
                                         "WHERE rowid = ?", (rid,))
                if not got:
                    continue
                row = got[0]
                values = dict(zip(common, row[1:]))
                if any(rid in entries and any(c not in values for c in icols)
                       or rid in entries and tuple(values[c] for c in icols) != entries[rid]
                       for icols, entries in listed.values()):
                    continue  # the backup's row with this rowid is not the row the index lists
                if (uid_at is not None and (name, str(row[1 + uid_at])) in tomb) or (
                        name == "tasks" and id_at is not None and (name, str(row[1 + id_at])) in tomb) or (
                        name == "task_links" and (name, f"{values.get('parent_id')}>{values.get('child_id')}") in tomb):
                    continue
                # An index proves the parent key, not the backup's latest payload
                # or exposure/taint history. Keep available custody, never restore
                # its old originality claim. Readable current salvage is unchanged.
                columns = common
                if name == "kanban_sync_parent_envelopes" and "identity_state" in dcols:
                    values["identity_state"] = "ambiguous"
                    columns = [*common, "identity_state"] if "identity_state" not in common else common
                    row = (rid, *(values[c] for c in columns))
                cur = dst.execute(f"INSERT OR IGNORE INTO {_q(name)} (rowid, {', '.join(_q(c) for c in columns)}) "
                                  f"VALUES ({', '.join('?' * (len(columns) + 1))})", row)
                filled += cur.rowcount
            result.tables.setdefault(name, TableLoss()).filled_from_backup = filled
    finally:
        bak.close()


def _rebuild_sequence(src: sqlite3.Connection, dst: sqlite3.Connection, tables: list[tuple[str, Optional[str]]],
                      fill: Optional[Path], floors: dict[str, int]) -> None:
    """``sqlite_sequence`` = max(old seq where readable, backup seq, max rowid now, highest rowid an
    index still lists) per AUTOINCREMENT table, so a lost row's id is never handed out again."""
    seqs: dict[str, int] = {}
    for conn in [src] + ([sqlite3.connect(f"{fill.resolve().as_uri()}?immutable=1", uri=True)] if fill else []):
        got, _ = _read_rows(conn, "SELECT name, seq FROM sqlite_sequence")
        for name, seq in got:
            with contextlib.suppress(TypeError, ValueError):
                seqs[str(name)] = max(seqs.get(str(name), 0), int(seq))
        if conn is not src:
            conn.close()
    dst.execute("DELETE FROM sqlite_sequence")
    for name, sql in tables:
        if not sql or "AUTOINCREMENT" not in sql.upper():
            continue
        top = dst.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {_q(name)}").fetchone()[0]
        dst.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                    (name, max(int(top), seqs.get(name, 0), floors.get(name, 0))))


# --- install ----------------------------------------------------------------------------------

def _data_version(watch: sqlite3.Connection) -> int:
    """``PRAGMA data_version`` changes when ANOTHER connection commits; it is only comparable on the
    same connection, so the recovery keeps one ``watch`` connection open from snapshot to install."""
    return int(watch.execute("PRAGMA data_version").fetchone()[0])


def _snapshot(watch: sqlite3.Connection, live: Path, dest: Path) -> tuple[sqlite3.Connection, int]:
    """Consistent copy of the live board (main file + WAL) read through ``watch`` with the backup
    API. Returns the watch connection to keep and its ``data_version`` from BEFORE the copy, so any
    commit after the copy started shows up as a changed version. When SQLite refuses to read the
    file, falls back to a raw copy of the main file + WAL with every connection of this process
    closed first (a raw read would cancel their POSIX locks) and a fresh watch opened after it."""
    dest.unlink(missing_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        version = _data_version(watch)
        watch.backup(out)
        return watch, version
    except sqlite3.DatabaseError:
        pass
    finally:
        out.close()
    watch.close()
    import shutil
    shutil.copyfile(live, dest)
    wal = live.with_name(live.name + "-wal")
    if wal.exists():
        # sqlite-sidecar-open: ok — corrupt-file fallback; `watch`, this process's connection, is closed above
        shutil.copyfile(wal, dest.with_name(dest.name + "-wal"))
    watch = sqlite3.connect(str(live), timeout=30)
    try:
        return watch, _data_version(watch)
    except sqlite3.DatabaseError:
        return watch, -1


def stranded_holders(live: Path, proc_root: Path = Path("/proc")) -> list[int]:
    """Pids of processes that still hold an fd on a DELETED ``-wal``/``-shm`` of ``live`` (Linux
    ``/proc``; empty elsewhere). Such a process keeps writing into its unlinked WAL and its next
    checkpoint copies those pages into the main file, over anything installed meanwhile: the
    install must wait until it is gone, or the board is corrupted again."""
    targets = {f"{live.resolve()}{suffix} (deleted)" for suffix in ("-wal", "-shm")}
    pids: list[int] = []
    try:
        procs = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return pids
    for proc in procs:
        try:
            fds = list((proc / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) in targets:
                    pids.append(int(proc.name))
                    break
            except OSError:
                continue
    return sorted(pids)


class InstalledUnverified(RuntimeError):
    """The rebuilt board was written into the live file but the post-install check failed."""


def _install(rebuilt: Path, live: Path) -> None:
    """Write ``rebuilt`` into ``live`` page by page with the backup API, inside SQLite's locking:
    the inode, the WAL and every other process's connection survive; they see the new content
    on their next read. Verified with integrity_check through a fresh connection. The backup is
    one write transaction, so a kill mid-copy (the child's alarm) leaves the old content."""
    src = sqlite3.connect(f"{rebuilt.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(live), timeout=60)
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
    finally:
        dst.close()
        src.close()
    state, messages = classify(live)
    if state != "ok":
        raise InstalledUnverified(f"installed board fails integrity_check: {messages[:3]}")


# Full-size damaged snapshots kept per board (newest first); older ones are deleted.
_SNAPSHOT_RETENTION = 3


def _rotate_snapshots(path: Path) -> None:
    snaps = sorted(path.parent.glob(f"{path.name}.recover-*.damaged.bak"), key=lambda p: p.name, reverse=True)
    for stale in snaps[_SNAPSHOT_RETENTION:]:
        for victim in (stale, stale.with_name(stale.name + "-wal"), stale.with_name(stale.name + "-shm")):
            with contextlib.suppress(OSError):
                victim.unlink(missing_ok=True)


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def recover(db_path: Path, *, fill: bool = True, file_card: bool = True, reconcile_sync: bool = True,
            dry_run: bool = False) -> RecoveryResult:
    """Recover ``db_path`` in THIS process. Run it in a process that holds no connection to the
    board (the CLI and :func:`spawn_recovery` do); see the module docstring for the steps."""
    path = Path(db_path).expanduser()
    with contextlib.suppress(OSError):
        path = path.resolve()  # one lock file per board, however the path was spelled
    result = RecoveryResult(status="failed", db_path=str(path), started_at=time.time())
    try:
        if not path.exists() or path.stat().st_size == 0:
            result.status = "missing"
            return result
        with recovery_lock(path) as held:
            if not held:
                result.status = "busy"
                result.reason = ("another recovery of this board is running "
                                 f"({describe_holder(recovery_lock_holder_info(path))})")
                return result
            state, messages = classify(path)
            result.messages = messages[:20]
            if state != "corrupt":
                result.status = "healthy" if state == "ok" else "index_only"
                result.reason = ("a clean process reads this board without errors; the caller may be "
                                 "stranded on deleted -wal/-shm files" if state == "ok" else
                                 "index-only damage: the connect-time guard repairs it with REINDEX")
                return result
            work = Path(tempfile.mkdtemp(prefix="kanban-recover-", dir=str(path.parent)))
            stamp = _stamp()
            snapshot = path.with_name(f"{path.name}.recover-{stamp}.damaged.bak")
            watch = sqlite3.connect(str(path), timeout=30)
            try:
                rebuilt = work / "rebuilt.db"
                for attempt in range(_MAX_SALVAGE_PASSES):
                    watch, version = _snapshot(watch, path, snapshot)
                    fill_src = _healthy_fill_source(path, {snapshot}) if fill else None
                    result.tables = {}
                    _build(snapshot, rebuilt, fill_src, result)
                    result.snapshot_path, result.fill_source = str(snapshot), str(fill_src) if fill_src else None
                    # A commit after the snapshot would be overwritten by the install: rebuild again.
                    # (Residual window: a commit between this check and the backup taking the write
                    # lock is still overwritten; the backup API can't check and lock atomically.)
                    if version >= 0 and _data_version(watch) == version:
                        break
                    _log.warning("kanban recovery: %s was written during rebuild pass %d; rebuilding again",
                                 path, attempt + 1)
                else:
                    # Still changing after the last pass: installing would drop those commits.
                    result.concurrent_writes = True
                    result.status = "contended"
                    result.reason = (f"the board was written during all {_MAX_SALVAGE_PASSES} rebuild passes; "
                                     "nothing installed (the next automatic attempt retries)")
                if result.status != "contended" and not dry_run:
                    result.stranded_pids = stranded_holders(path)
                    if result.stranded_pids:
                        result.status = "stranded_holders"
                        result.reason = (f"pid(s) {result.stranded_pids} still hold deleted -wal/-shm files of this "
                                         "board; their checkpoint would overwrite the install. Restart them, "
                                         "then run `hermes kanban recover` again. Nothing installed.")
                if dry_run and result.status not in ("contended",):
                    result.status = "dry_run"
                    return result
                if result.status == "failed":
                    try:
                        _install(rebuilt, path)
                        result.status = "recovered"
                    except InstalledUnverified as exc:
                        result.status = "installed_unverified"
                        result.reason = str(exc)
            finally:
                watch.close()
                import shutil
                shutil.rmtree(work, ignore_errors=True)
                _rotate_snapshots(path)
            if result.status in ("recovered", "installed_unverified"):
                _invalidate_init_cache(path)
            result.finished_at = time.time()
            result.report_path = str(_write_report(path, result, stamp))
        if result.status in ("recovered", "installed_unverified"):
            _log.error("kanban board %s %s after b-tree corruption: %d known lost row(s); report %s",
                       path, result.status, result.lost_rows, result.report_path)
        else:
            _log.error("kanban board %s NOT recovered (%s): %s; report %s",
                       path, result.status, result.reason, result.report_path)
        if reconcile_sync and result.status == "recovered":
            result.sync = _reconcile_sync(path)
        if file_card and result.status in ("recovered", "installed_unverified"):
            result.card_id = _file_card(path, result)
        if result.report_path:
            Path(result.report_path).write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
    except Exception as exc:  # health: allow BLE001 -- the result carries the failure and the status says what was written
        if result.status not in ("recovered", "installed_unverified"):
            result.status = "failed"  # nothing was written to the live board
        result.reason = f"{type(exc).__name__}: {exc}"
        _log.exception("kanban board %s recovery %s", path, result.status)
    result.finished_at = result.finished_at or time.time()
    return result


def _invalidate_init_cache(path: Path) -> None:
    with contextlib.suppress(Exception):
        from hermes_cli import kanban_db_connect as kbc
        with kbc._INIT_LOCK:
            kbc._INITIALIZED_PATHS.discard(str(path.resolve()))


def _write_report(path: Path, result: RecoveryResult, stamp: str) -> Path:
    report = path.with_name(f"{path.name}.recovery-{stamp}.json")
    report.write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
    return report


def _board_slug(path: Path) -> Optional[str]:
    with contextlib.suppress(Exception):
        from hermes_cli import kanban_db_sync as sync
        return sync.board_slug_for_path(str(path.resolve()))
    return None


def _reconcile_sync(path: Path) -> Optional[dict]:
    """Bring the sync service and the recovered board back in step (best effort, never raises)."""
    try:
        from hermes_cli import kanban_db_sync as sync
        if not sync.sync_enabled() or not sync.board_selected(str(path.resolve())):
            return None
        from hermes_cli import kanban_sync_client as ksc
        return ksc.reconcile(_board_slug(path) or "default", path)
    except Exception as exc:  # health: allow BLE001 -- reported in the card; reconcile can be rerun by hand
        return {"error": f"{type(exc).__name__}: {exc}"}


def _summary_lines(result: RecoveryResult) -> list[str]:
    lines = []
    for name, loss in sorted(result.tables.items()):
        if (loss.known_lost or loss.possibly_lost or loss.filled_from_backup or loss.unreadable_paths
                or loss.stale_index_entries):
            keys = f" e.g. {loss.lost_keys[:5]}" if loss.lost_keys else ""
            unmeasured = (f", {loss.unreadable_paths} read path(s) stopped on damage (rows no index lists "
                          "are not counted)" if loss.unreadable_paths else "")
            stale = f", {loss.stale_index_entries} stale index entr(ies) ignored" if loss.stale_index_entries else ""
            lines.append(f"- {name}: {loss.recovered} read, {loss.filled_from_backup} filled from backup, "
                         f"{loss.known_lost} known lost ({loss.possibly_lost} possibly){keys}{unmeasured}{stale}")
    return lines or ["- no table lost a row an index still lists"]


def _file_card(path: Path, result: RecoveryResult) -> Optional[str]:
    """A card on the recovered board so the loss is reconciled, not discovered by chance."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.config import load_config
        from hermes_cli.kanban_db_connect import connect
        assignee = str(((load_config() or {}).get("kanban") or {}).get("recovery_card_assignee") or "default")
        body = "\n".join([
            f"The board file `{path}` failed integrity_check with table b-tree damage and was rebuilt "
            "automatically (hermes_cli/kanban_db_recover.py) and written back in place.",
            "",
            f"- Known lost rows: {result.lost_rows}. Report: `{result.report_path}`",
            f"- Damaged copy kept at `{result.snapshot_path}`; rows filled from `{result.fill_source}`.",
            f"- Concurrent writes during the rebuild: {result.concurrent_writes}. Sync reconcile: {result.sync}",
            "",
            *_summary_lines(result),
            "",
            "Do: 1) read the report and recreate each known-lost card or run from its other copies (sync replica "
            "`hermes kanban sync diff`, peer gateways, task_events); 2) if sync reconcile failed, run "
            "`hermes kanban sync reconcile`; 3) use the five-whys skill on why the board was damaged (look "
            "for processes holding deleted kanban.db-wal/-shm files).",
        ])
        conn = connect(path)
        try:
            return kb.create_task(
                conn, title=f"Kanban board auto-recovered from corruption: {result.lost_rows} known lost row(s)",
                body=body, assignee=assignee, created_by="kanban-recover",
                idempotency_key=f"kanban-recover:{path.name}:{Path(result.report_path or '').name}",
            )
        finally:
            conn.close()
    except Exception as exc:  # health: allow BLE001 -- the report and the ERROR log still carry the loss
        _log.warning("kanban recovery: could not file the follow-up card (%s: %s)", type(exc).__name__, exc)
        return None


# --- spawning from a live process ---------------------------------------------------------------

_SPAWNED: dict[str, float] = {}
# One automatic attempt per board per this many seconds (a failed rebuild is not retried in a loop).
AUTO_RETRY_SECONDS = 1800


def auto_recover_enabled() -> bool:
    try:
        from hermes_cli.config import load_config
        value = ((load_config() or {}).get("kanban") or {}).get("auto_recover_corruption", True)
    except Exception:  # health: allow BLE001 -- unreadable config: keep the default
        value = True
    return bool(value)


def spawn_recovery(db_path: Path, *, wait: bool = False, timeout: float = RECOVERY_TIMEOUT_SECONDS,
                   extra_args: Optional[list[str]] = None) -> Optional[dict]:
    """Run :func:`recover` in a fresh interpreter (see module docstring, step 1). With ``wait`` the
    parsed result is returned; otherwise the child runs detached and ``None`` is returned.
    Rate-limited to one automatic start per board per :data:`AUTO_RETRY_SECONDS`."""
    key = str(Path(db_path).expanduser().resolve())
    now = time.monotonic()
    if not wait and now - _SPAWNED.get(key, -AUTO_RETRY_SECONDS) < AUTO_RETRY_SECONDS:
        return None
    _SPAWNED[key] = now
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_TASK")}
    holder_task = os.environ.get(HOLDER_TASK_ENV) or os.environ.get("HERMES_KANBAN_TASK")
    if holder_task:
        env[HOLDER_TASK_ENV] = holder_task
    cmd = [sys.executable, "-m", "hermes_cli.kanban_db_recover", "--db", key, "--json", *(extra_args or [])]
    from hermes_cli._startup_fast import project_root_str
    repo_root = project_root_str()
    env["PYTHONPATH"] = repo_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if not wait:
        log = Path(key).with_name(Path(key).name + ".recover.log")
        with open(log, "ab") as out:
            subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env,
                             # detached-spawn: ok — board recovery must finish its backup/swap even if the process that noticed the damage dies [daveey]
                             cwd=repo_root, start_new_session=True)
        _log.error("kanban board %s has table b-tree damage; started automatic recovery (log %s)", key, log)
        return None
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                              timeout=timeout, env=env, cwd=repo_root, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        # The child's own alarm normally ends it first; either way an unfinished backup is one
        # uncommitted transaction, so the live board keeps its old content.
        return {"status": "failed", "db_path": key, "reason": f"recovery timed out after {exc.timeout:.0f}s"}
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"status": "failed", "db_path": key, "reason": (done.stderr or done.stdout)[-2000:]}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.kanban_db_recover")
    parser.add_argument("--db", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-fill", action="store_true")
    parser.add_argument("--no-card", action="store_true")
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--timeout", type=int, default=RECOVERY_TIMEOUT_SECONDS - 30)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with contextlib.suppress(ImportError):
        from agent.delegation_context import kanban_path_is_fenced
        if kanban_path_is_fenced(Path(args.db)):
            print(json.dumps({"status": "failed", "db_path": args.db,
                              "reason": "delegated child contexts may not rewrite a Kanban board"}))
            return 1
    # Bounded: a writer that never releases its lock would otherwise keep the backup retrying forever
    # (holding the recovery lock). The handler raises so ``recover``'s cleanup (work dir, report)
    # runs; an unfinished backup is one uncommitted transaction, so nothing half-written lands.
    if args.timeout > 0 and hasattr(__import__("signal"), "SIGALRM"):
        import signal

        def _timed_out(_signum, _frame):
            raise TimeoutError(f"recovery exceeded {args.timeout}s")

        signal.signal(signal.SIGALRM, _timed_out)  # windows-footgun: ok — guarded by hasattr above
        signal.alarm(args.timeout)
    result = recover(Path(args.db), fill=not args.no_fill, file_card=not args.no_card,
                     reconcile_sync=not args.no_sync, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result.to_dict(), default=str))
    else:
        print(f"{result.status}: {result.db_path} {result.reason}".rstrip())
        print("\n".join(_summary_lines(result)))
        if result.report_path:
            print(f"report: {result.report_path}")
    return 0 if result.status in ("recovered", "healthy", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
