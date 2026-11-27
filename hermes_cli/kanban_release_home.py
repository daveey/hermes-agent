"""Authenticated HOME consume and final canonical application queue.

Consumed-only proof never grants coverage. Final application uses immutable
PREPARED wire, authenticated private canonical certification and atomic remote
materialization; executor transfer and capability rollout remain separate gates.
"""
from __future__ import annotations

import logging

from hermes_cli import kanban_release_certificate as cert, kanban_release_intent as intent
from hermes_cli import kanban_release_home_queue as queue

_log = logging.getLogger(__name__)


def _hub():
    from hermes_cli import kanban_sync_client as client
    return client.Hub(client.load_identity())


def _identity(conn, hub, row, own, board):
    from hermes_cli import kanban_operator_call as operator
    if (hub.ident.gateway != own or operator._own_gateway(conn) != own or operator._board_slug(conn) != board
            or row["board"] != board or not operator._is_home(conn, own, row["task_id"])):
        raise ValueError("HOME release identity changed")


def _authenticate(conn, hub, row, own, board):
    ev = hub.row_evidence(board, "task_events", row["delivery_uid"], max_response_bytes=cert.MAX_BYTES)
    if (not isinstance(ev, dict) or ev.get("board") != board or ev.get("table") != "task_events"
            or ev.get("uid") != row["delivery_uid"] or ev.get("state") != "live"):
        raise ValueError("canonical HOME release carrier unavailable")
    canonical = ev.get("row")
    p = queue.validate(canonical, board, own)
    # Compare all frozen fields available in the real local event schema. Board
    # alone supplies HLC/origin/run UID; private intent supplies actor authority.
    for k in ("uid", "task_id", "payload", "created_at"):
        expected = row["delivery_uid"] if k == "uid" else row[k]
        if cert.encode(canonical[k]) != cert.encode(expected):
            raise ValueError("HOME release carrier differs from captured event")
    _identity(conn, hub, row, own, board)
    return p


def _proof(hub, body):
    from hermes_cli.kanban_release_native import _proof as read_proof
    return read_proof(hub, body)


def _freeze(conn, row, p):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_sync import uuid7
    op = {"client_op_id": uuid7(), "op": "consume_owner_release", "task": row["task_id"],
          "intent_uid": row["intent_uid"], "digest": p["digest"], "seen_comments": p["seen_comments"]}
    with kb.write_txn(conn):
        current = conn.execute(f"SELECT * FROM {queue.TABLE} WHERE delivery_uid=?", (row["delivery_uid"],)).fetchone()
        if current is None or tuple(current) != tuple(row):
            raise ValueError("HOME release intake changed before preparation")
        if conn.execute(f"SELECT 1 FROM {queue.TABLE} WHERE board=? AND intent_uid=? AND stage!='observed' "
                        "AND delivery_uid!=?", (row["board"], row["intent_uid"], row["delivery_uid"])).fetchone():
            raise ValueError("HOME release intent already has an authenticated delivery")
        conn.execute(f"UPDATE {queue.TABLE} SET stage='prepared',consume_wire=?,wire_digest=? "
                     "WHERE delivery_uid=? AND stage='observed' AND consume_wire IS NULL",
                     (cert.encode(op), intent.digest(op), row["delivery_uid"]))


def _transition(conn, row, expected, stage):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        changed = conn.execute(f"UPDATE {queue.TABLE} SET stage=? WHERE delivery_uid=? AND stage=? "
                               "AND consume_wire=? AND wire_digest=?", (stage, row["delivery_uid"], expected,
                                                                        row["consume_wire"], row["wire_digest"]))
        if changed.rowcount != 1:
            raise ValueError("HOME release consume stage changed")


def _reconcile(conn, hub, row, body, op, own, board):
    from hermes_cli import kanban_release_replica as replica
    receipt = cert.validate(hub.owner_release_consumption(board, body["intent_uid"]), board=board,
                            task=body["task_id"], intent_uid=body["intent_uid"])
    if cert.encode(receipt["consume"]) != cert.encode(op) or cert.encode(receipt["intent"]) != cert.encode(body):
        raise ValueError("HOME release winning consumption differs")
    _identity(conn, hub, row, own, board)
    replica._store(conn, receipt)  # historical cache only; no coverage consumers
    _transition(conn, row, "consume_pending", "consumed_pending_apply")


def _process(conn, hub, row, own, board):
    if row['stage'] == 'consumed_pending_apply':
        from hermes_cli import kanban_release_final_home
        return kanban_release_final_home.process(conn, hub, row, own, board)
    _identity(conn, hub, row, own, board)
    p = cert.parse(row["payload"])
    body = intent.validate(p["intent"])
    # Resolve the frozen identity before mutable TTL/comments/owner checks.
    op = queue.wire(row) if row["stage"] != "observed" else None
    if op is not None:
        proof = _proof(hub, body)
        if proof["state"] == "consumed":
            if row["stage"] == "prepared":
                _transition(conn, row, "prepared", "consume_pending")
                row = conn.execute(f"SELECT * FROM {queue.TABLE} WHERE delivery_uid=?", (row["delivery_uid"],)).fetchone()
            _reconcile(conn, hub, row, body, op, own, board)
            return
    else:
        p = _authenticate(conn, hub, row, own, board)
        body = p["intent"]
        proof = _proof(hub, body)
    if proof["state"] != "authorized":
        return  # unavailable certificate/refusal/expiry never remints a request
    if op is None:
        _freeze(conn, row, p)
        row = conn.execute(f"SELECT * FROM {queue.TABLE} WHERE delivery_uid=?", (row["delivery_uid"],)).fetchone()
        op = queue.wire(row)
    # A later canonical edit/deletion cannot piggyback an earlier authorization.
    _authenticate(conn, hub, row, own, board)
    if row["stage"] == "prepared":
        _transition(conn, row, "prepared", "consume_pending")
        row = conn.execute(f"SELECT * FROM {queue.TABLE} WHERE delivery_uid=?", (row["delivery_uid"],)).fetchone()
    _identity(conn, hub, row, own, board)
    hub.ops(board, [op])  # never a writer across HTTP; ACK alone is not consumed
    _reconcile(conn, hub, row, body, op, own, board)


def tick(conn, rep, own, board):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_release import Refusal
    from hermes_cli.kanban_sync_client import HubError
    queue.intake(conn, rep, board, own)
    if not queue.exists(conn):
        return
    completed = ''
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_release_applied_receipts'").fetchone():
        # Presence suppresses repeat work, never confers clearance. Corrupt
        # private receipts remain held by the APPLIED verifier, not retried into
        # replacement certification or another final operation.
        completed = ' AND NOT EXISTS(SELECT 1 FROM owner_release_applied_receipts a WHERE a.intent_uid=q.intent_uid)'
    rows = conn.execute(f"SELECT * FROM {queue.TABLE} q WHERE board=? AND stage IN ('observed','prepared','consume_pending','consumed_pending_apply') "
                        f"{completed} ORDER BY checked,rowid LIMIT 2", (board,)).fetchall()
    if not rows:
        return
    import sqlite3
    from hermes_cli.kanban_sync_client import SyncUnavailable
    try:
        hub = _hub()
    except (OSError, HubError, SyncUnavailable) as exc:
        _log.info("HOME release transport unavailable: %s", exc)
        return
    try:
        for row in rows:
            # Durable round-robin: transient failure cannot monopolize intake.
            with kb.write_txn(conn):
                conn.execute(f"UPDATE {queue.TABLE} SET checked=(SELECT COALESCE(MAX(checked),0)+1 FROM {queue.TABLE}) "
                             "WHERE delivery_uid=?", (row["delivery_uid"],))
            row = conn.execute(f"SELECT * FROM {queue.TABLE} WHERE delivery_uid=?", (row["delivery_uid"],)).fetchone()
            try:
                _process(conn, hub, row, own, board)
            except (Refusal, ValueError, OSError, HubError, sqlite3.Error) as exc:
                _log.info("HOME release %s remains held: %s", row["delivery_uid"], exc)
    finally:
        hub.close()
