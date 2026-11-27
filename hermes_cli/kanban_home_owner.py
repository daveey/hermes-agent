"""Explicit cooperative handoff owner; never an automatic migration sweep.

Only actual credential-pinned Hub transport responses enter local finalizers.
A caller-provided manifest/ACK dictionary is never accepted as a network reply.
The operator CLI retains all source/destination eligibility and custody checks.
"""
from __future__ import annotations

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as source
from hermes_cli import kanban_home_destination as destination
from hermes_cli.kanban_sync_client import Hub

_KEYS = ("task", "source", "destination", "nonce", "expected_version")
_PHASES = ("prepared", "committed", "materialized", "complete", "aborted")


def _identity(conn, hub: Hub, board: str, identity: dict, actor: str) -> dict:
    if not isinstance(hub, Hub) or conn.in_transaction:
        raise ValueError("home owner requires real Hub transport outside a writer")
    if not isinstance(identity, dict) or set(identity) != set(_KEYS):
        raise ValueError("home owner needs one exact identity tuple")
    state = conn.execute("SELECT board_id,gateway_id FROM kanban_sync_state").fetchall()
    if (len(state) != 1 or tuple(state[0]) != (board, identity[actor])
            or hub.ident.gateway != identity[actor]):
        raise ValueError("home owner gateway/board mismatch")
    return dict(identity)


def _advance(hub: Hub, board: str, identity: dict, step: str, rows=None) -> dict:
    body = {**identity, "step": step}
    if rows is not None:
        body["rows"] = rows
    record = hub.home_transfer(board, body)
    if (not isinstance(record, dict) or any(type(record.get(k)) is not type(v) or record[k] != v for k,v in identity.items())
            or record.get("phase") not in _PHASES or not isinstance(record.get("rows"), list)):
        raise ValueError("authenticated home response identity/shape mismatch")
    return record


def source_step(conn, hub: Hub, board: str, identity: dict) -> dict:
    """Prepare/fence -> Board commit, or durable local STOP -> Board STOP.

    An unknown response retains the SAME immutable tuple. No timeout release,
    fresh nonce, local rollback of already committed protocol or lease repair.
    """
    identity = _identity(conn, hub, board, identity, "source")
    args = {k: v for k, v in identity.items() if k != "task"}
    source.validate_identity(identity["task"], **args)
    # No protocol bytes have left yet: an absent/rolled-back Board must leave
    # ordinary local work writable. Never infer release after a mutation send.
    cap = hub.home_transfer_capability(board)
    if not isinstance(cap, dict) or type(cap.get("home_transfer")) is not int or cap["home_transfer"] != 1:
        raise ValueError("Board home-transfer capability v1 required before local preparation")
    with dbc.write_txn(conn):
        source.prepare(conn, identity["task"], **args)
    record = _advance(hub, board, identity, "prepare")
    if record["phase"] == "prepared":
        record = _advance(hub, board, identity, "commit")
    if record["phase"] == "aborted":
        with dbc.write_txn(conn):
            source.abort_source(conn, record)
        return record
    if record["phase"] in ("materialized", "complete"):
        with dbc.write_txn(conn):
            source.finalize_source(conn, record)
        record = _advance(hub, board, identity, "stop", record["rows"])
    return record


def destination_step(conn, hub: Hub, board: str, identity: dict) -> dict:
    """Read authenticated manifest -> actual committed import/readback -> ACK.

    No network operation runs inside the native writer. If the ACK is lost,
    retry re-reads actual durable materialization before sending the same ACK.
    """
    identity = _identity(conn, hub, board, identity, "destination")
    record = _advance(hub, board, identity, "read")
    if record["phase"] in ("prepared", "complete", "aborted"):
        # A complete response is historical custody, not a request to re-import
        # an old manifest over later legitimate execution. Native eligibility
        # releases only when the newer authenticated STOP feed arrives.
        return record
    with dbc.write_txn(conn):
        rows = destination.materialize(conn, record)
    return _advance(hub, board, identity, "materialize", rows)


def reconcile_precommit(conn, hub: Hub, board: str, identity: dict) -> dict:
    """Request a durable Board NONCOMMIT decision for this exact attempt.

    Never infer a refusal from an exception/timeout or clear a local marker. If
    a commit won (including a lost reply), Board refuses abort and we stay frozen.
    If abort won, delayed packets for that tuple can never commit afterward.
    """
    identity = _identity(conn, hub, board, identity, "source")
    record = _advance(hub, board, identity, "abort")
    with dbc.write_txn(conn):
        source.abort_source(conn, record)
    return record
