"""Host-local quarantine for containment receipts whose evidence deterministically conflicts. [daveey]

Certification stays strict. Only a ``ContainmentConflict`` quarantines: the Board's authenticated evidence for a
receipt differs from this host's first certified observation (``identity_changed``), or a feed event differs from
its canonical receipt (``event_differs``). Every other refusal (unproven or non-live evidence, unavailable proof,
an invalid page) re-raises, so the per-board backoff (``kanban_sync_refusal``) retries the whole page with the
cursor where it was: a transient evidence failure never becomes a permanent hold.

A quarantined receipt is journaled, with its wire row, before the cursor can move. Every hold row names the receipt
it derives from (``direct``, or ``dependency`` for descendants), so a task stays held while ANY unreleased receipt
names it. A held task and its dependency descendants cannot dispatch, be picked up or push (their own chat-link and
attachment deletes included); their later feed rows are withheld. Other tasks keep syncing.

Nothing releases a hold automatically. ``hermes kanban sync quarantine --release`` (``kanban_sync_quarantine_release``)
appends an operator acceptance of the Board's current live evidence, replays the withheld rows through certify and
apply, and lifts only that receipt's holds inside that apply transaction. The first observed receipt is never
rewritten and acceptances are append-only. One hub card per receipt tells the owner
(``kanban_sync_quarantine_notice``). This ledger is never synced authority and never fabricates a containment episode.
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
# Released per receipt by ``lift`` (its triggers are dropped and restored inside that transaction).
_TABLES = {
    "containment_sync_quarantine": "uid TEXT PRIMARY KEY,task_id TEXT NOT NULL,board TEXT NOT NULL,kind TEXT NOT NULL,"
                                   "reason TEXT NOT NULL,wire TEXT NOT NULL",
    "containment_sync_quarantine_tasks": "task_id TEXT NOT NULL,receipt_uid TEXT NOT NULL,via TEXT NOT NULL,"
                                         "PRIMARY KEY(task_id,receipt_uid)",
    "containment_sync_withheld": "wire TEXT PRIMARY KEY",
}
# Append-only forever: the durable audit record of each operator release.
_ACCEPTANCES = ("containment_sync_acceptances", "receipt_uid TEXT NOT NULL,sha256 TEXT NOT NULL,evidence TEXT NOT NULL,"
                "reason TEXT NOT NULL,operator TEXT NOT NULL,accepted_at INTEGER NOT NULL,"
                "PRIMARY KEY(receipt_uid,sha256,accepted_at)")
# The owning task of a deleted chat link/attachment: its outbox delete carries no fields and its row is gone.
_OWNER = "containment_sync_delete_owner"
_OWNED = ("task_chat_links", "task_attachments")
_ADOPTED = ("containment_sync_quarantine", "containment_sync_quarantine_tasks", _ACCEPTANCES[0])
_CLOSURE = ("WITH RECURSIVE held(id) AS (SELECT task_id FROM containment_sync_quarantine_tasks{where} UNION "
            "SELECT l.child_id FROM task_links l JOIN held h ON l.parent_id=h.id) SELECT id FROM held")


def _exists(conn, table: str = "containment_sync_quarantine") -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def file_holds(path: Path) -> bool:
    """Cheap peek: does this database file hold any quarantine row? Opens no pooled shadow connection. Fails
    closed: an unreadable file answers True, so the caller adopts through the real connection (or the round fails
    before its first send)."""
    try:
        # Short-lived read-only peek, closed below; never a pooled handle.
        peek = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)  # noqa: kanban-lockguard
    except sqlite3.Error:
        return True
    try:
        return _exists(peek) and peek.execute("SELECT 1 FROM containment_sync_quarantine LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return True
    finally:
        peek.close()


def _immutable(conn, table: str) -> None:
    for op in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()} BEFORE {op} ON {table} "
                     "BEGIN SELECT RAISE(ABORT,'containment_quarantine_immutable'); END")


def install(conn) -> None:
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli.kanban_containment_connections import verifier
    from hermes_cli import kanban_containment_replica as replica
    guard.install(conn, verify_release=lambda r, s: verifier(conn, r, s))
    replica.install(conn)
    for table, columns in (*_TABLES.items(), _ACCEPTANCES):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
        _immutable(conn, table)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_OWNER} (table_name TEXT NOT NULL,row_uid TEXT NOT NULL,"
                 "task_id TEXT NOT NULL,PRIMARY KEY(table_name,row_uid))")
    for table in _OWNED:
        if {"uid", "task_id"} <= {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {_OWNER}_{table} AFTER DELETE ON {table} BEGIN "
                         f"INSERT OR REPLACE INTO {_OWNER} VALUES ('{table}',OLD.uid,OLD.task_id); END")


def receipts(conn) -> list[str]:
    if not _exists(conn):
        return []
    return [r[0] for r in conn.execute("SELECT uid FROM containment_sync_quarantine ORDER BY rowid")]


def tasks(conn, receipt: str | None = None, *, without: str | None = None) -> set[str]:
    """Held tasks: every task a hold row names, and its dependency descendants. ``receipt``: only that receipt's;
    ``without``: as if that receipt were released. UNION (not UNION ALL) also terminates for cyclic graphs."""
    if not _exists(conn):
        return set()
    where, args = ("", ()) if receipt is None and without is None else (
        (" WHERE receipt_uid=?", (receipt,)) if receipt is not None else (" WHERE receipt_uid<>?", (without,)))
    return {r[0] for r in conn.execute(_CLOSURE.format(where=where), args)}


def held(conn, task: str) -> bool:
    return task in tasks(conn)


def holders(conn, task: str) -> list[str]:
    """The quarantined receipts holding ``task``, directly or as a dependency descendant."""
    return sorted(uid for uid in receipts(conn) if task in tasks(conn, uid))


def entries(conn) -> list[dict]:
    """Owner-visible view: one dict per quarantined receipt, with every task it holds."""
    if not _exists(conn):
        return []
    return [{"receipt": uid, "board": board, "task": task, "kind": kind, "reason": reason,
             "held_tasks": sorted(tasks(conn, uid))}
            for uid, task, board, kind, reason in conn.execute(
                "SELECT uid,task_id,board,kind,reason FROM containment_sync_quarantine ORDER BY rowid")]


def release_command(board: str, receipt: str) -> str:
    return f"hermes kanban --board {board} sync quarantine --release {receipt} --reason ..."


def refusal(conn, task: str) -> str | None:
    """Why a held card waits and how it is released (``resume-contained`` cannot: this is not an episode)."""
    mine = holders(conn, task)
    if not mine:
        return None
    board = conn.execute("SELECT board FROM containment_sync_quarantine WHERE uid=?", (mine[0],)).fetchone()[0]
    return (f"held by sync quarantine of receipt {', '.join(mine)}; release with "
            f"`{release_command(board, mine[0])}` once you have ruled on the Board's copy")


def lift(conn, receipt: str, replayed: list[str], deferred: list[str]) -> None:
    """Inside the caller's write transaction: drop ``receipt``'s quarantine row and every hold row it derives, the
    withheld wires just replayed, and keep ``deferred`` wires withheld for a later release."""
    saved = {}
    for table in _TABLES:
        for op in ("update", "delete"):
            name = f"{table}_{op}"
            saved[name] = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()[0]
            conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DELETE FROM containment_sync_quarantine_tasks WHERE receipt_uid=?", (receipt,))
    conn.execute("DELETE FROM containment_sync_quarantine WHERE uid=?", (receipt,))
    conn.executemany("DELETE FROM containment_sync_withheld WHERE wire=?", [(w,) for w in replayed])
    conn.executemany("INSERT OR IGNORE INTO containment_sync_withheld VALUES (?)", [(w,) for w in deferred])
    if not receipts(conn):
        conn.execute(f"DELETE FROM {_OWNER}")
    for sql in saved.values():
        conn.execute(sql)


def subjects(conn, table: str, uid: str, fields: dict) -> set[str]:
    if table == "tasks":
        return {uid}
    if table == "task_links":
        parent, sep, child = uid.partition(">")
        return {parent, child} if sep else {fields.get("parent_id"), fields.get("child_id")} - {None}
    task = fields.get("task_id")
    if task:
        return {task}
    # Delete envelopes have no fields. Read the existing row (or, for a chat link/attachment deleted here, the
    # owner its delete recorded), never infer a task from an event uid. Unknown tables keep the board refusal.
    if table not in ("task_runs", "task_events", "task_comments", *_OWNED):
        raise ContainmentRefused(f"quarantine cannot identify {table}/{uid}")
    row = conn.execute(f"SELECT task_id FROM {table} WHERE uid=?", (uid,)).fetchone()
    if row is None and table in _OWNED and _exists(conn, _OWNER):
        row = conn.execute(f"SELECT task_id FROM {_OWNER} WHERE table_name=? AND row_uid=?", (table, uid)).fetchone()
    return {row[0]} if row else set()


def wire(change) -> str:
    return json.dumps(dataclasses.asdict(change), sort_keys=True, separators=(",", ":"), allow_nan=False)


def protected(change) -> bool:
    return change.table == "task_events" and change.fields.get("kind") in ("contained", "containment_released")


def certified_page(conn, hub, board: str, changes) -> list:
    """Transport seam around (not weakening) certify_page; run before apply.

    Each conflict is durable before a cursor can move. If journaling fails, the exception aborts the round and no
    cursor advances. A later apply failure may conservatively retain a quarantine, never lose one.
    """
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli.kanban_containment_lifecycle import ContainmentConflict
    for change in changes:
        if not protected(change):
            continue
        try:
            replica.certify_page(conn, hub, board, [change])
        except ContainmentConflict as exc:
            _quarantine(conn, board, change, exc)
    return _withhold(conn, changes)


def _quarantine(conn, board: str, change, exc) -> None:
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    held_tasks = subjects(conn, change.table, change.uid, change.fields)
    cached = replica._cached(conn, change.uid)
    if cached:
        held_tasks.add(cached["row"]["task_id"])
    if not held_tasks:
        raise exc  # no task identity: fail the board round rather than skip uncertain history
    reason = str(exc) if exc.uid in (None, change.uid) else f"{exc} (receipt {exc.uid})"
    with kb.write_txn(conn, allow_nested=True):
        install(conn)
        inserted = conn.execute("INSERT OR IGNORE INTO containment_sync_quarantine VALUES (?,?,?,?,?,?)",
                                (change.uid, sorted(held_tasks)[0], board, exc.kind, reason, wire(change))).rowcount
        conn.executemany("INSERT OR IGNORE INTO containment_sync_quarantine_tasks VALUES (?,?,'direct')",
                         [(task, change.uid) for task in held_tasks])
    if inserted:
        log.error("kanban sync: quarantined receipt %s on %s tasks=%s (%s: %s); unrelated rows keep syncing; "
                  "release with `%s`", change.uid, board, sorted(held_tasks), exc.kind, reason,
                  release_command(board, change.uid))
    _mirror(conn)


def _withhold(conn, changes) -> list:
    from hermes_cli import kanban_db as kb
    if not tasks(conn):
        return changes
    with kb.write_txn(conn, allow_nested=True):
        install(conn)
        for receipt in receipts(conn):
            # New dependency edges can precede their tasks. Persist their downstream holds, per receipt, even
            # though withholding that edge means it is not in task_links.
            mine = tasks(conn, receipt)
            grown = True
            while grown:
                before = len(mine)
                mine |= {ch.uid.partition(">")[2] for ch in changes
                         if ch.table == "task_links" and ">" in ch.uid and ch.uid.partition(">")[0] in mine}
                grown = len(mine) != before
            conn.executemany("INSERT OR IGNORE INTO containment_sync_quarantine_tasks VALUES (?,?,'dependency')",
                             [(task, receipt) for task in mine])
        blocked = tasks(conn)
        safe = []
        for ch in changes:
            names = subjects(conn, ch.table, ch.uid, ch.fields)
            # A delete with no known subject could be an unknown deleted child of a held task: withhold it too.
            if names & blocked or (not names and ch.op != "upsert"):
                conn.execute("INSERT OR IGNORE INTO containment_sync_withheld VALUES (?)", (wire(ch),))
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
        try:
            names = subjects(conn, row["table_name"], row["row_uid"], fields or {})
        except ContainmentRefused:
            continue  # unidentifiable: stays pending, never pushed
        if names and not names & blocked:
            safe.append(row)
        elif not names and row.get("op") == "delete":
            # A chat link/attachment deleted before this file had a quarantine left no owner record; removing it
            # cannot release a hold, so one quarantined task does not pin every unrelated delete pending.
            safe.append(row)
    return safe


def snapshot(conn) -> dict:
    """Carry local safety state across retention rebuilds; called before closing."""
    if not _exists(conn):
        return {}
    data = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in (*_TABLES, _ACCEPTANCES[0])}
    data["receipts"] = [r[0] for r in conn.execute("SELECT evidence FROM containment_canonical_receipts")]
    return data


def restore(conn, data: dict, checkpoint: Path | None = None) -> None:
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
        for table in (*_TABLES, _ACCEPTANCES[0]):
            for row in data.get(table, ()):
                marks = ",".join("?" for _ in row)
                conn.execute(f"INSERT OR IGNORE INTO {table} VALUES ({marks})", row)
    if checkpoint is not None:
        # The data now lives in the database; a later open must not re-certify every receipt again.
        checkpoint.with_suffix(".quarantine.json").unlink(missing_ok=True)


def adopt(source, dest) -> None:
    """Copy the replica's new hold and acceptance rows into the live file before repair/pickup; no release
    authority. Only rows the destination lacks are written (no write transaction when there are none), and the
    withheld ledger is never copied: each file replays only the rows its own pull withheld."""
    if source is dest or not _exists(source):
        return
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    if replica._board(source) != replica._board(dest):
        raise ContainmentRefused("quarantine belongs to a different board")
    # Holds must not depend on replaying receipts: the destination can correctly
    # retain a different first observation of that uid. Never replace its cache.
    new = {}
    for table in _ADOPTED:
        have = {tuple(r) for r in dest.execute(f"SELECT * FROM {table}")} if _exists(dest, table) else set()
        new[table] = [row for row in map(tuple, source.execute(f"SELECT * FROM {table}")) if row not in have]
    if not any(new.values()):
        return
    with kb.write_txn(dest, allow_nested=True):
        install(dest)
        for table, rows in new.items():
            for row in rows:
                dest.execute(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' for _ in row)})", row)


def notify(live, board: str) -> list[str]:
    from hermes_cli.kanban_sync_quarantine_notice import notify as file_cards
    return file_cards(live, board)


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
