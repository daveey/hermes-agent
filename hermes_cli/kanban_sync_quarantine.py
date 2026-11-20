"""Host-local, append-only quarantine for refused containment receipts. [daveey]

Certification remains strict. A refused receipt and every withheld wire row are
retained before advancing the feed cursor. Its task and dependency descendants
cannot dispatch, be imported, or push. Other tasks keep syncing. There is no
automatic release: corrected evidence must not erase an observed identity conflict.
This ledger is never synced authority and never fabricates a containment episode.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import json
import logging
import os
import sqlite3
from pathlib import Path

from hermes_cli.kanban_containment_lifecycle import ContainmentRefused

log = logging.getLogger(__name__)
_SINK = contextvars.ContextVar("containment_quarantine_sink", default=None)
_TABLES = {
    "containment_sync_quarantine": "uid TEXT PRIMARY KEY,task_id TEXT NOT NULL,board TEXT NOT NULL,reason TEXT NOT NULL,wire TEXT NOT NULL",
    "containment_sync_quarantine_tasks": "task_id TEXT PRIMARY KEY,receipt_uid TEXT NOT NULL",
    "containment_sync_withheld": "wire TEXT PRIMARY KEY",
}


def _exists(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='containment_sync_quarantine'").fetchone() is not None


def file_holds(path: Path) -> bool:
    """Cheap peek: does this database file hold any quarantine row? Opens no pooled shadow connection."""
    import sqlite3
    try:
        peek = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return False
    try:
        return _exists(peek) and peek.execute("SELECT 1 FROM containment_sync_quarantine LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        peek.close()


def install(conn) -> None:
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli.kanban_containment_connections import verifier
    from hermes_cli import kanban_containment_replica as replica
    guard.install(conn, verify_release=lambda r, s: verifier(conn, r, s))
    replica.install(conn)
    for table, columns in _TABLES.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
        for op in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()} BEFORE {op} ON {table} "
                         "BEGIN SELECT RAISE(ABORT,'containment_quarantine_immutable'); END")


def tasks(conn) -> set[str]:
    if not _exists(conn):
        return set()
    # A dependent cannot inherit readiness from a stale terminal projection of a
    # quarantined parent. UNION (not UNION ALL) also terminates for cyclic graphs.
    return {r[0] for r in conn.execute(
        "WITH RECURSIVE held(id) AS (SELECT task_id FROM containment_sync_quarantine_tasks UNION "
        "SELECT l.child_id FROM task_links l JOIN held h ON l.parent_id=h.id) SELECT id FROM held")}


def held(conn, task: str) -> bool:
    return task in tasks(conn)


def _subjects(conn, table: str, uid: str, fields: dict) -> set[str]:
    if table == "tasks":
        return {uid}
    if table == "task_links":
        parent, sep, child = uid.partition(">")
        return {parent, child} if sep else {fields.get("parent_id"), fields.get("child_id")} - {None}
    task = fields.get("task_id")
    if task:
        return {task}
    # Delete envelopes have no fields. Read the existing row, never infer a task
    # from an event uid. Unknown tables/subjects retain the board-level refusal.
    if table not in ("task_runs", "task_events", "task_comments", "task_attachments", "task_chat_links"):
        raise ContainmentRefused(f"quarantine cannot identify {table}/{uid}")
    row = conn.execute(f"SELECT task_id FROM {table} WHERE uid=?", (uid,)).fetchone()
    return {row[0]} if row else set()


def _wire(change) -> str:
    return json.dumps(dataclasses.asdict(change), sort_keys=True, separators=(",", ":"), allow_nan=False)


def certified_page(conn, hub, board: str, changes) -> list:
    """Transport seam around (not weakening) certify_page; run before apply.

    Each refusal is durable before a cursor can move. If journaling fails, the
    exception aborts the round and no cursor advances. A later apply failure may
    conservatively retain a quarantine, never lose one.
    """
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    for change in changes:
        if change.table != "task_events" or change.fields.get("kind") not in ("contained", "containment_released"):
            continue
        try:
            replica.certify_page(conn, hub, board, [change])
        except ContainmentRefused as exc:
            subjects = _subjects(conn, change.table, change.uid, change.fields)
            cached = replica._cached(conn, change.uid)
            if cached:
                subjects.add(cached["row"]["task_id"])
            if not subjects:
                raise  # no task identity: fail the board round rather than skip uncertain history
            with kb.write_txn(conn, allow_nested=True):
                install(conn)
                inserted = conn.execute("INSERT OR IGNORE INTO containment_sync_quarantine VALUES (?,?,?,?,?)",
                                        (change.uid, sorted(subjects)[0], board, str(exc), _wire(change))).rowcount
                conn.executemany("INSERT OR IGNORE INTO containment_sync_quarantine_tasks VALUES (?,?)",
                                 [(task, change.uid) for task in subjects])
            if inserted:
                log.error("kanban sync: quarantined receipt %s on %s tasks=%s: %s; unrelated rows keep syncing",
                          change.uid, board, sorted(subjects), exc)
            _mirror(conn)
    blocked = tasks(conn)
    if not blocked:
        return changes
    with kb.write_txn(conn, allow_nested=True):
        install(conn)
        # New dependency edges can precede their tasks. Persist their downstream
        # holds even though withholding that edge means it is not in task_links.
        again = True
        while again:
            before = len(blocked)
            for ch in changes:
                if ch.table == "task_links":
                    parent, sep, child = ch.uid.partition(">")
                    if sep and parent in blocked:
                        blocked.add(child)
            again = len(blocked) != before
        for task in blocked:
            conn.execute("INSERT OR IGNORE INTO containment_sync_quarantine_tasks VALUES (?,?)", (task, "dependency"))
        safe = []
        for ch in changes:
            subjects = _subjects(conn, ch.table, ch.uid, ch.fields)
            if subjects & blocked:
                conn.execute("INSERT OR IGNORE INTO containment_sync_withheld VALUES (?)", (_wire(ch),))
            elif not subjects and ch.op != "upsert":
                # Could be an unknown deleted child of a held task. Keep it, too.
                conn.execute("INSERT OR IGNORE INTO containment_sync_withheld VALUES (?)", (_wire(ch),))
            else:
                safe.append(ch)
    _mirror(conn)
    return safe


def sendable(conn, rows: list[dict], *, blocked: set[str] | None = None) -> list[dict]:
    blocked = tasks(conn) if blocked is None else blocked
    if not blocked:
        return rows
    safe = []
    for row in rows:
        fields = row["fields"]
        fields = json.loads(fields) if isinstance(fields, str) else fields
        subjects = _subjects(conn, row["table_name"], row["row_uid"], fields or {})
        if subjects and not subjects & blocked:
            safe.append(row)
    return safe


def snapshot(conn) -> dict:
    """Carry local safety state across retention rebuilds; called before closing."""
    if not _exists(conn):
        return {}
    data = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in _TABLES}
    data["receipts"] = [r[0] for r in conn.execute("SELECT evidence FROM containment_canonical_receipts")]
    return data


def restore(conn, data: dict) -> None:
    if not data:
        return
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    # Validate the preserved receipts again, exactly as offline pickup does. These
    # bytes came from the protected local cache, never from a feed row.
    proofs = {proof["uid"]: proof for proof in map(json.loads, data["receipts"])}
    for proof in proofs.values():
        stop = None
        if proof["row"]["kind"] == "containment_released":
            payload = json.loads(proof["row"]["payload"])
            stop = proofs[payload["episode"]]
        replica.remember(conn, proof, board=proof["board"], task=proof["row"]["task_id"], uid=proof["uid"], stop_evidence=stop)
    with kb.write_txn(conn, allow_nested=True):
        install(conn)
        for table in _TABLES:
            for row in data[table]:
                marks = ",".join("?" for _ in row)
                conn.execute(f"INSERT OR IGNORE INTO {table} VALUES ({marks})", row)


def adopt(source, dest) -> None:
    """Transfer host-local quarantine before live repair/pickup; no release authority."""
    if source is dest or not _exists(source):
        return
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    if replica._board(source) != replica._board(dest):
        raise ContainmentRefused("quarantine belongs to a different board")
    # Holds must not depend on replaying receipts: the destination can correctly
    # retain a different first observation of that uid. Never replace its cache.
    with kb.write_txn(dest, allow_nested=True):
        install(dest)
        for table in _TABLES:
            for row in source.execute(f"SELECT * FROM {table}"):
                marks = ",".join("?" for _ in row)
                dest.execute(f"INSERT OR IGNORE INTO {table} VALUES ({marks})", tuple(row))


@contextlib.contextmanager
def mirroring(live):
    """A page publishes safety holds before a later page can fail or advance."""
    token = _SINK.set(live)
    try:
        yield
    finally:
        _SINK.reset(token)


def _mirror(source) -> None:
    dest = _SINK.get()
    if dest is not None:
        adopt(source, dest)


def pending_batch(conn, now: int, limit: int) -> list[dict]:
    """Limit after filtering: a held FIFO prefix cannot starve another task."""
    blocked = tasks(conn)
    if not blocked:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM kanban_sync_outbox WHERE state='pending' AND (retry_after IS NULL OR retry_after<=?) "
            "ORDER BY created_at,rowid LIMIT ?", (now, limit))]
    cursor = conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE state='pending' AND (retry_after IS NULL OR retry_after<=?) "
        "ORDER BY created_at,rowid", (now,))
    batch = []
    for row in cursor:
        batch.extend(sendable(conn, [dict(row)], blocked=blocked))
        if len(batch) >= limit:
            break
    return batch


def save_rebuild(conn, path: Path) -> None:
    """Durable checkpoint BEFORE unlink: survive a crash before replacement opens."""
    data = snapshot(conn)
    if not data:
        return
    target = path.with_suffix(".quarantine.json")
    temp = target.with_suffix(f".json.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(target)
    # The directory rename must be durable before the database can be unlinked.
    fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_rebuild(path: Path) -> dict:
    try:
        return json.loads(path.with_suffix(".quarantine.json").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
