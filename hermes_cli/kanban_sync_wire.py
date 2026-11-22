"""Pure outbox-to-wire translation shared by sync senders and fixtures. [daveey]"""
import json

from hermes_cli import kanban_db_sync as sync


def wire_fields(table: str, fields: dict) -> dict:
    """Only the synced wire columns (a column a later migration added is not sent until it is declared)."""
    allowed = {sync._wire_name(table, c) for c in sync.SYNCED_COLUMNS[table]}
    allowed |= sync.VIRTUAL_WIRE_FIELDS.get(table, frozenset())
    return {k: v for k, v in fields.items() if k in allowed}


def translate(row: dict, hlc: str) -> dict:
    """One outbox row -> one row op; semantic retain keeps immutable request bytes."""
    if row["op"] == "retain":
        from hermes_cli.kanban_retain_transport import frozen_wire
        return frozen_wire(row)
    op = {"client_op_id": row["op_id"], "table": row["table_name"], "uid": row["row_uid"], "hlc": hlc}
    if row["op"] == "delete":
        return {**op, "op": "delete"}
    fields = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
    if row["op"] == "release":
        return {**fields, "client_op_id": row["op_id"], "op": "release", "task": row["row_uid"]}
    out = {**op, "op": "upsert", "fields": wire_fields(row["table_name"], fields)}
    if row["op"] in ("import", "compat_import") and row["table_name"] == "tasks":
        out["import"] = True
    return out
