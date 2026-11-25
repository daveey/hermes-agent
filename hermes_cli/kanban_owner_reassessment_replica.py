"""Durable, append-only replica of Board-certified owner reassessment receipts.

Host-local, never synced. Only a ``Hub.row_evidence`` read performed *here* (or, for
offline pickup copies, an already-certified row re-certified from the source replica)
is stored; payload projections, sender journals and request JSON are never inputs.
A stored receipt is re-certified from its own evidence on every read. Nothing here
suppresses a detector or advertises a capability.
"""
from __future__ import annotations

import contextvars
import sqlite3

from hermes_cli import kanban_owner_reassessment_canonical as canonical
from hermes_cli.kanban_containment_functions import register as register_function
from hermes_cli.kanban_owner_reassessment import ReassessmentRefused

TABLE = "owner_reassessment_canonical_receipts"
_PERMIT: contextvars.ContextVar[sqlite3.Connection | None] = contextvars.ContextVar("owner_reassessment_cache_write", default=None)


class ReassessmentConflict(ReassessmentRefused):
    """A different canonical identity arrived for an already-recorded receipt."""


def register(conn: sqlite3.Connection) -> None:
    register_function(conn, "kanban_owner_reassessment_cache_write", 0,
                      lambda: int(_PERMIT.get() is conn and conn.in_transaction))


def install(conn: sqlite3.Connection) -> None:
    register(conn)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (event_uid TEXT PRIMARY KEY, board TEXT NOT NULL, "
                 "task_id TEXT NOT NULL, episode_uid TEXT NOT NULL UNIQUE, run_uid TEXT NOT NULL, "
                 "gateway TEXT NOT NULL, evidence TEXT NOT NULL)")
    abort = "BEGIN SELECT RAISE(ABORT,'owner reassessment canonical receipt is immutable'); END"
    for op in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_{op.lower()} BEFORE {op} ON {TABLE} {abort}")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_insert BEFORE INSERT ON {TABLE} "
                 f"WHEN NOT kanban_owner_reassessment_cache_write() {abort}")
    # REPLACE deletes without DELETE triggers: refuse every key/rowid conflict before it resolves.
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_replace BEFORE INSERT ON {TABLE} "
                 f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE event_uid=NEW.event_uid "
                 f"OR episode_uid=NEW.episode_uid OR rowid=NEW.rowid) {abort}")


def _board(conn: sqlite3.Connection) -> str | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone():
        return None
    row = conn.execute("SELECT board_id FROM kanban_sync_state LIMIT 1").fetchone()
    return row[0] if row else None


def _store(conn: sqlite3.Connection, evidence: dict, receipt: dict) -> None:
    """Insert exact evidence. Private: callers certify first (see :func:`certify`)."""
    if _board(conn) != receipt["board"]:
        raise ReassessmentRefused("canonical reassessment receipt belongs to a different board")
    raw = canonical.canonical_bytes(evidence)
    install(conn)
    from hermes_cli import kanban_db as kb
    uid = receipt["park_event"]["uid"]
    values = (uid, receipt["board"], receipt["task_id"], receipt["episode_uid"],
              receipt["park_event"]["run_uid"], receipt["gateway"], raw)
    with kb.write_txn(conn, allow_nested=True):
        prior = conn.execute(f"SELECT event_uid,board,task_id,episode_uid,run_uid,gateway,evidence FROM {TABLE} "
                             "WHERE event_uid=? OR episode_uid=?", (uid, receipt["episode_uid"])).fetchall()
        if prior:
            if (len(prior) != 1 or tuple(prior[0])[:6] != values[:6]
                    or canonical.certified_identity(canonical.parse_evidence(prior[0][6])) != canonical.certified_identity(evidence)):
                raise ReassessmentConflict("canonical reassessment identity changed")
            return  # append-only: the first observation is kept
        token = _PERMIT.set(conn)
        try:
            conn.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?)", values)
        finally:
            _PERMIT.reset(token)


def certify(conn: sqlite3.Connection, hub, board: str, *, task: str, uid: str, sender=None) -> dict:
    """Fetch the Board's own evidence (outside any writer transaction), certify, then record.

    Any transport error propagates and records nothing: unavailable proof never authorizes.
    ``sender`` is the sender's receipt; direct delivery certification passes it so the Board's
    attestation must equal it exactly and the enrolled sender gateway must be the original one.
    """
    if conn.in_transaction:
        raise ReassessmentRefused("canonical reassessment fetch cannot run inside a writer transaction")
    evidence = hub.row_evidence(board, "task_events", uid, max_response_bytes=canonical.MAX_EVIDENCE_BYTES)
    receipt = canonical.certified_receipt(evidence, board=board, task=task, uid=uid, sender=sender)
    if sender is not None and getattr(getattr(hub, "ident", None), "gateway", None) != receipt["gateway"]:
        raise ReassessmentRefused("receipt was not originally admitted through this enrolled gateway")
    _store(conn, evidence, receipt)
    return receipt


def verified_receipt(conn: sqlite3.Connection, event_uid: str | None) -> dict | None:
    """Reopened durable proof, re-certified from stored evidence; None when absent or invalid.

    Callers must still check :func:`canonical.event_matches` against their event row and
    :func:`canonical.current_matches` against the current task before relying on it.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    row = conn.execute(f"SELECT board,task_id,episode_uid,run_uid,gateway,evidence FROM {TABLE} WHERE event_uid=?",
                       (event_uid,)).fetchone()
    if row is None or row[0] != _board(conn):
        return None
    try:
        receipt = canonical.certified_receipt(canonical.parse_evidence(row[5]), board=row[0], task=row[1], uid=event_uid)
    except ReassessmentRefused:
        return None
    if (row[2], row[3], row[4]) != (receipt["episode_uid"], receipt["park_event"]["run_uid"], receipt["gateway"]):
        return None
    return receipt


def latest_park_uid(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Newest live park event uid of the card in this replica; None when unreadable (fail closed)."""
    try:
        # Local execution DBs physically remove tombstones and have no deleted_at;
        # shadow replicas may retain that column. Never require one schema's shape.
        columns = {r[1] for r in conn.execute('PRAGMA table_info(task_events)')}
        live = ' AND deleted_at IS NULL' if 'deleted_at' in columns else ''
        rows = conn.execute('SELECT uid,created_at,id FROM task_events WHERE task_id=? '
                            'AND kind IN (?,?)' + live + ' ORDER BY created_at DESC LIMIT 129',
                            (task_id, *canonical.PARK_KINDS)).fetchall()
        if not rows:
            return None
        newest_second = rows[0][1]
        candidates = [dict(zip(('uid', 'created_at', 'id'), r)) for r in rows if r[1] == newest_second]
        if len(candidates) > 128:
            return None  # ambiguous over-bound tie, never guess from arrival order
        from hermes_cli.kanban_sync_divergence import _event_order
        # Local ids are arrival order, not portable authority. Even v7 events
        # can share one millisecond: ambiguity must never select an old park.
        ordered = [(_event_order(row)[:2], row['uid']) for row in candidates]
        top = max(key for key, _uid in ordered)
        winners = [uid for key, uid in ordered if key == top]
        return winners[0] if len(winners) == 1 else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def verified_for_event(conn: sqlite3.Connection, event: dict, task: dict | None = None,
                       *, now: int | None = None) -> dict | None:
    """Accessor for the scanner: receipt only if the event matches it.

    With ``task`` (and ``now``) the receipt must also be the card's *current* park: newest park
    event in this replica, scheduled without a run, before its backstop. Without ``task`` the
    result is historical proof only and must not suppress anything.
    """
    receipt = verified_receipt(conn, event.get("uid")) if isinstance(event, dict) else None
    if receipt is None or not canonical.event_matches(event, receipt):
        return None
    if task is not None and not canonical.current_matches(
            receipt, task, latest_park_uid=latest_park_uid(conn, receipt["task_id"]), now=now):
        return None
    return receipt


def _is_candidate(ch) -> bool:
    if ch.table != "task_events" or ch.op != "upsert" or ch.fields.get("kind") not in canonical.PARK_KINDS:
        return False
    try:
        payload = canonical.parse_evidence(ch.fields.get("payload") or "")
    except ReassessmentRefused:
        return False
    return isinstance(payload, dict) and "owner_reassessment_episode_uid" in payload


def certify_page(conn: sqlite3.Connection, hub, board: str, changes) -> None:
    """Prewriter seam (same signature as containment's). Fetch before the writer transaction.

    A park event merely *claiming* an episode is ordinary history: if the Board attests nothing
    for it, nothing is recorded and nothing is authorized (page still applies). Transport errors
    abort the page; an attestation that contradicts the feed row or a recorded receipt raises.
    """
    for ch in changes:
        if not _is_candidate(ch):
            continue
        evidence = hub.row_evidence(board, "task_events", ch.uid, max_response_bytes=canonical.MAX_EVIDENCE_BYTES)
        if not isinstance(evidence, dict) or "owner_reassessment_v1" not in evidence:
            continue
        try:
            receipt = canonical.certified_receipt(evidence, board=board, task=ch.fields.get("task_id"), uid=ch.uid)
        except ReassessmentRefused:
            continue  # malformed/lookalike attestation: no proof, no authority
        if not canonical.event_matches({**ch.fields, "uid": ch.uid, "origin_gateway": ch.origin_gateway}, receipt):
            raise ReassessmentConflict("replica event differs from its canonical reassessment receipt")
        _store(conn, evidence, receipt)


def adopt_receipts(conn: sqlite3.Connection, source: sqlite3.Connection, changes) -> None:
    """Offline pickup: carry already-recorded receipts from the pulled replica, re-certified."""
    for ch in changes:
        if not _is_candidate(ch):
            continue
        row = source.execute(f"SELECT evidence FROM {TABLE} WHERE event_uid=?", (ch.uid,)).fetchone() \
            if source.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone() else None
        if row is None:
            continue
        try:
            evidence = canonical.parse_evidence(row[0])
            receipt = canonical.certified_receipt(evidence, board=evidence.get("board"), task=ch.fields.get("task_id"),
                                                  uid=ch.uid)
        except (ReassessmentRefused, AttributeError):
            continue  # corrupt/foreign source row: carry nothing
        if not canonical.event_matches({**ch.fields, "uid": ch.uid, "origin_gateway": ch.origin_gateway}, receipt):
            raise ReassessmentConflict("replica event differs from its canonical reassessment receipt")
        _store(conn, evidence, receipt)
