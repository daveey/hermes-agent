"""Dormant cooperative handoff owner; no CLI/sweep/automatic activation yet.

Only actual credential-pinned Hub transport responses enter local finalizers.
A caller-provided manifest/ACK dictionary is never accepted as a network reply.
Finish destination pre-STOP eligibility and typed capture gates before rollout.
"""
from __future__ import annotations

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as source
from hermes_cli import kanban_home_destination as destination
from hermes_cli.kanban_sync_client import Hub

_KEYS = ("task", "source", "destination", "nonce", "expected_version")
_PHASES = ("prepared", "committed", "materialized", "complete")


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
    with dbc.write_txn(conn):
        source.prepare(conn, identity["task"], **{k:v for k,v in identity.items() if k != "task"})
    record = _advance(hub, board, identity, "prepare")
    if record["phase"] == "prepared":
        record = _advance(hub, board, identity, "commit")
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
    if record["phase"] in ("prepared", "complete"):
        # A complete response is historical custody, not a request to re-import
        # an old manifest over later legitimate execution. Native eligibility
        # releases only when the newer authenticated STOP feed arrives.
        return record
    with dbc.write_txn(conn):
        rows = destination.materialize(conn, record)
    return _advance(hub, board, identity, "materialize", rows)
