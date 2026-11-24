"""Pending local edits win over remote fields until the Board answers. [daveey]"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)


def pending_task_fields(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Wire fields per task with a local edit still in the outbox (not rejected, and not a replica op being undone:
    a ``discarding`` op whose undo keeps failing must not keep the card off the Board's next version)."""
    out: dict[str, set[str]] = {}
    for uid, fields in conn.execute("SELECT row_uid, fields FROM kanban_sync_outbox WHERE table_name = 'tasks' "
                                    "AND op IN ('upsert', 'import') AND state NOT IN ('rejected', 'discarding')"):
        out.setdefault(uid, set()).update(json.loads(fields))
    return out


def without_pending(changes: list[sync.RemoteChange], pending: dict[str, set[str]]) -> list[sync.RemoteChange]:
    """A field with an unanswered local edit keeps the local value until that edit's answer arrives
    (RFC "Offline queue and reconnect": pull first, local-pending fields win locally)."""
    out = []
    for ch in changes:
        keep = pending.get(ch.uid) if ch.table == "tasks" and ch.op == "upsert" else None
        if keep:
            ch = sync.RemoteChange(ch.seq, ch.table, ch.uid, ch.op, {k: v for k, v in ch.fields.items() if k not in keep},
                                   ch.version, ch.hlc, ch.origin_gateway, ch.client_op_id)
        out.append(ch)
    return out


# Not ``kanban_sync_%``: ``drop_triggers`` (a sync-off open) and the trigger drift check own that prefix.
REPLICA_GUARD = "replica_outbox_readonly"
REPLICA_TOMBSTONE_GUARD = "replica_tombstone_readonly"
_GUARD_SQL = (
    f"CREATE TRIGGER IF NOT EXISTS {REPLICA_GUARD} BEFORE INSERT ON kanban_sync_outbox "
    "BEGIN SELECT RAISE(IGNORE); END",
    f"CREATE TRIGGER IF NOT EXISTS {REPLICA_TOMBSTONE_GUARD} BEFORE INSERT ON kanban_sync_tombstones "
    f"WHEN {sync._NOT_APPLYING} BEGIN SELECT RAISE(IGNORE); END",
)


def guard_replica(conn: sqlite3.Connection) -> None:
    """Make a replica keep no trace of a write the feed apply did not make. [daveey]

    On a replica only the capture triggers write the outbox and tombstones (the direct outbox writers: queue,
    compat, transports, divergence repair, run on live files), and they skip ``applying``, so every op or tombstone a
    replica could record comes from a write the pull did not make. Nothing sends a replica's outbox, and a pending op
    masks its field from every later pull (:func:`pending_task_fields`): the 2026-11-23 loop
    (:func:`discard_replica_writes`). A tombstone makes the apply skip the Board's row for good. Both inserts are
    skipped, not refused: replicas also take legitimate local writes outside ``applying`` (the containment schema
    install and repair, connect-time data migrations), which must keep working. With neither kept, a stray write no
    longer sticks: the row takes the Board's next version as any pulled row does, and divergence repair sees the gap
    meanwhile. The pull's own tombstones are written under ``applying`` and kept. Installed once per file; the passive
    checkpoint after it is best-effort, so the schema change usually lands now rather than as a later main-file
    rewrite that re-arms the links plan."""
    have = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?)",
                                           (REPLICA_GUARD, REPLICA_TOMBSTONE_GUARD))}
    if len(have) == len(_GUARD_SQL):
        return
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        for sql in _GUARD_SQL:
            conn.execute(sql)
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


# ``unknown`` is no row and no tombstone on the Board (below its history floor): for a replica, as gone as the rest.
_GONE = ("never_existed", "deleted", "unknown")
_EVIDENCE_PER_ROUND = 64  # Board reads per pull; a larger backlog drains over later rounds
_NEXT_READ: dict[str, int] = {}  # replica path -> where the next round's reads start, so no op starves at the back
_SHOWN = 10  # unresolved keys named per warning
_FAILED_READS_PER_ROUND = 3  # past this the Board is taken as down for the round


def _op_keys(table: str, uid: str, fields: str) -> list[tuple[str, str]]:
    """The Board rows an op's write touched: its own row, and the card of a card-owned row."""
    keys = [(table, uid)]
    if table == "task_links":
        keys.append(("tasks", uid.partition(">")[2]))
    elif table != "tasks":
        try:
            task = (json.loads(fields or "{}") or {}).get("task_id")
        except (ValueError, AttributeError):  # malformed, or not an object: the op's own row still resolves it
            task = None
        if task:
            keys.append(("tasks", str(task)))
    return keys


def _delete_change(table: str, uid: str) -> sync.RemoteChange:
    return sync.RemoteChange(seq=0, table=table, uid=uid, op="delete", fields={}, version=0, hlc="",
                             origin_gateway="")


def _read_evidence(hub: Any, board: str, keys: list, path: str) -> tuple[dict, Exception | None]:
    """At most :data:`_EVIDENCE_PER_ROUND` Board reads, starting where the last round stopped. A key whose read fails
    is skipped, so one row the Board will not answer for cannot hold back every op after it;
    :data:`_FAILED_READS_PER_ROUND` failures end the round. Each round starts past the keys the last one tried, so
    rows that keep failing, or ops a quarantine keeps withheld, cannot starve the ops behind them."""
    start = _NEXT_READ.get(path, 0) % len(keys)
    evidence: dict = {}
    failure: Exception | None = None
    failures = tried = 0
    for key in (keys[start:] + keys[:start])[:_EVIDENCE_PER_ROUND]:
        tried += 1
        try:
            reply = hub.row_evidence(board, *key)
            if not isinstance(reply, dict):
                raise TypeError(f"Board evidence for {key} is {type(reply).__name__}, not an object")
            evidence[key] = reply
        except Exception as exc:  # health: allow BLE001 -- a failed read defers that key's ops to a later round
            failure, failures = exc, failures + 1
            if failures >= _FAILED_READS_PER_ROUND:
                break
    _NEXT_READ[path] = start + tried
    return evidence, failure


def _restore_page(evidence: dict, state: dict, keys: dict) -> list[sync.RemoteChange]:
    """The Board's own rows for ``keys`` as one feed page: upserts of rows it holds (runs before cards), then deletes
    of rows it does not hold."""
    from hermes_cli.kanban_sync_client import _row_change
    upserts = sorted((_row_change(table, evidence[(table, uid)]["row"]) for table, uid in keys
                      if state[(table, uid)] == "live"), key=lambda ch: ch.table == "tasks")
    return upserts + [_delete_change(table, uid) for table, uid in keys if state[(table, uid)] in _GONE]


def discard_replica_writes(conn: sqlite3.Connection, hub: Any, board: str) -> int:
    """Undo the local writes a replica captured, from the Board's own rows; returns the ops resolved.

    A replica (``sync-shadow/<board>.db``) only pulls: nothing ever sends its outbox. Before :func:`guard_replica` it
    carried live capture, so a write to it outside the feed apply queued an op that stayed ``pending`` forever, and
    the pull (:func:`pending_task_fields`) then kept that field's local value over every Board echo. Hub incident
    2026-11-23: the legacy in-flight run backfill (``kanban_db_connect._backfill_legacy_inflight_runs``, kept off
    replicas since 6be38658) gave the hub, a2 and a3 replicas a ``running`` run the Board never had and pointed
    t_01a2082680c38239 at it. On the hub the masked ``current_run_uid`` made divergence repair re-send the live value
    every round (~30 s), raising the card's Board version each time. The producer fix stopped new contamination but
    left the queued ops in place.

    Every row an op touched (and its card; ``task_links`` by ``parent>child``) is read from the Board, at most
    :data:`_EVIDENCE_PER_ROUND` per round; a row whose read fails waits for a later round. An op is resolved once all
    its rows are read and the quarantine filter passes their restore (a withheld restore keeps the op until the
    quarantine is released). Only resolved ops' rows are touched: rows the Board holds are applied again, rows it does
    not hold (deleted, never had, or below its history floor) are deleted through the feed's delete handlers, runs
    before cards, deletes last. Before the apply, in one transaction: the resolved ops leave ``pending`` (a pending op
    makes the apply keep the local row), a held run's ``sync_version`` is cleared (a stray update leaves it at the
    Board's version, which the run fence then treats as already applied), any replica tombstone a stray delete wrote
    for a held row is removed. The restore page re-points a held card at the Board's run in the apply; after it, with
    the ops' deletion, any card still naming a gone run stops naming it (a backstop: nulled before the apply, a
    failed apply would leave a card on no run at all). The ops are deleted only after the apply commits, so a failure
    anywhere leaves them for the next round, marked ``discarding`` so they stop masking the card; an op the
    quarantine withholds goes back to ``pending`` and waits as it was. A failure after the reads is logged, not
    raised, so the feed pull that follows still runs, and a page that fails is retried one op at a time, so one row
    that always fails holds back only its own op. Evidence is not kept between rounds, so an op resolves when all
    its rows fall in one round's reads.

    A withheld restore is written to the quarantine's withheld ledger like any withheld feed row and replayed at its
    release, which is harmless: card upserts are version-fenced, a run upsert still meets its pending op or
    ``sync_version``, and the deletes are of rows the Board does not hold. A delete of a row the replica no longer
    holds names no card, so the quarantine withholds it while any quarantine on the board is open; it only costs
    that op a Board read per round until then, and masks nothing (only card ops mask a field). [daveey]"""
    from pathlib import Path
    from hermes_cli.kanban_db_connect import _main_db_file
    path = _main_db_file(conn)
    if not path or Path(path).parent.name != sync.SHADOW_DIR:
        return 0
    ops = [(op_id, _op_keys(table, uid, fields)) for op_id, table, uid, fields in
           conn.execute("SELECT op_id, table_name, row_uid, fields FROM kanban_sync_outbox ORDER BY rowid")]
    if not ops:
        return 0
    keys = list(dict.fromkeys(key for _, op_keys in ops for key in op_keys))
    evidence, failure = _read_evidence(hub, board, keys, str(path))
    try:
        return _undo(conn, hub, board, ops, keys, evidence, failure)
    except Exception:  # health: allow BLE001 -- a failed undo must not stop the feed pull that follows
        _log.warning("kanban sync: %s replica: undoing its local ops as one page failed; trying them one at a time",
                     board, exc_info=True)
    done = 0
    for op in ops:  # one row that always fails must not hold back the rest
        try:
            done += _undo(conn, hub, board, [op], op[1], evidence, failure, quiet=True)
        except Exception as exc:  # health: allow BLE001 -- that op stays for the next round; the feed pull still runs
            _log.warning("kanban sync: %s replica: undoing op %s (rows %s) failed (%r); kept for the next round",
                         board, op[0], op[1][:_SHOWN], exc)
    return done


def _undo(conn: sqlite3.Connection, hub: Any, board: str, ops: list, keys: list, evidence: dict,
          failure: Exception | None, *, quiet: bool = False) -> int:
    """The second half of :func:`discard_replica_writes`, from the read Board rows: certify, undo, drop the ops."""
    from hermes_cli import kanban_sync_quarantine as quarantine
    from hermes_cli.kanban_db_connect import write_txn
    state = {key: ev.get("state") if ev.get("state") != "live" or ev.get("row") else None
             for key, ev in evidence.items()}
    known = ("live", *_GONE)
    attested = [(op_id, op_keys) for op_id, op_keys in ops if all(state.get(key) in known for key in op_keys)]
    changes = _restore_page(evidence, state, dict.fromkeys(key for _, op_keys in attested for key in op_keys))
    todo = quarantine.certified_page(conn, hub, board, changes) if changes else []
    # An op whose restore the containment quarantine withholds waits with it: undone now, it would leave its rows
    # neither stray nor the Board's until the quarantine is released.
    withheld = {(ch.table, ch.uid) for ch in changes} - {(ch.table, ch.uid) for ch in todo}
    done = [(op_id, op_keys) for op_id, op_keys in attested if not withheld.intersection(op_keys)]
    held = [(op_id,) for op_id, op_keys in attested if withheld.intersection(op_keys)]
    if held:  # an earlier failed page may have marked them; a withheld op waits as it was, masking as before
        with write_txn(conn):
            conn.executemany("UPDATE kanban_sync_outbox SET state = 'pending' WHERE op_id = ? AND state = 'discarding'",
                             held)
    if not done:
        if quiet:  # one op of a retried page: the page's own warning covers it
            return 0
        waiting = [key for key in keys if state.get(key) not in known or key in withheld][:_SHOWN]
        _log.warning("kanban sync: %s replica holds %d local op(s); none resolved this round (%s)", board, len(ops),
                     failure or f"unattested or withheld {waiting}")
        return 0
    resolved = [op_id for op_id, _ in done]
    rows = dict.fromkeys(key for _, op_keys in done for key in op_keys)  # only the resolved ops' rows are undone
    todo = [ch for ch in todo if (ch.table, ch.uid) in rows]
    live = [key for key in rows if state[key] == "live"]
    gone = [key for key in rows if state[key] in _GONE]
    with write_txn(conn):
        conn.executemany("UPDATE kanban_sync_outbox SET state = 'discarding' WHERE op_id = ?",
                         [(op_id,) for op_id in resolved])
        for table, uid in live:
            conn.execute("DELETE FROM kanban_sync_tombstones WHERE table_name = ? AND row_uid = ?", (table, uid))
            if table == "task_runs":
                conn.execute("UPDATE task_runs SET sync_version = NULL WHERE uid = ?", (uid,))
    gone_runs = [row[0] for table, uid in gone if table == "task_runs"
                 for row in conn.execute("SELECT id FROM task_runs WHERE uid = ?", (uid,))]
    cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
    sync.apply_remote_changes(conn, todo, new_cursor=cursor, board=board, observers=False)
    with write_txn(conn):
        # Backstop after the apply, not before: the restore page already re-points a held card; nulled before the
        # apply, a failed apply would leave a card on no run at all instead of its stray one.
        conn.executemany("UPDATE tasks SET current_run_id = NULL WHERE current_run_id = ?",
                         [(run_id,) for run_id in gone_runs])
        conn.executemany("DELETE FROM kanban_sync_outbox WHERE op_id = ?", [(op_id,) for op_id in resolved])
    unresolved = len(ops) - len(resolved)
    _log.warning("kanban sync: %s replica held %d local op(s) nothing sends; resolved %d: restored %d row(s) from "
                 "the Board, deleted %d it does not hold; %d left for later rounds%s", board, len(ops),
                 len(resolved), len(live), len(gone), unresolved, f" ({failure})" if failure else "")
    return len(resolved)
