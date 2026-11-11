"""Local custody of fields omitted for an older Board, independent of outbox retirement."""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping


def held_fields(conn: sqlite3.Connection, table: str, uid: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT field FROM kanban_sync_omissions WHERE table_name=? AND row_uid=? AND state!='delivered'",
        (table, uid))}


def without_held(conn: sqlite3.Connection, table: str, uid: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Run under the authoritative-apply write lock, including merged replies without pending-wins."""
    held = held_fields(conn, table, uid)
    _queue_restore(conn, table, uid, fields, held)
    return {k: v for k, v in fields.items() if k not in held}


def _queue_restore(conn, table, uid, authoritative, held):
    """Presence is a capability hint, not an acknowledgement; try once per omission."""
    import json
    import time
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_sync_rejections import _local
    wanted = held & authoritative.keys()
    if not wanted:
        return
    row = _local(conn, table, uid)
    if row is None:
        return
    current = client._row_fields(conn, table, row)
    for field in wanted:
        receipts = conn.execute(
            "SELECT original_op_id,restore_op_id FROM kanban_sync_omissions "
            "WHERE table_name=? AND row_uid=? AND field=? AND state!='delivered'", (table, uid, field)).fetchall()
        if any(r[1] for r in receipts) or field not in current:
            continue
        op_id = sync.uuid7()
        payload = {field: current[field]}
        if table != 'tasks':
            routing = ('parent_id', 'child_id') if table == 'task_links' else ('task_id',)
            payload.update({key: current[key] for key in routing if key in current})
        conn.execute(
            "INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
            "VALUES (?,?,?,'restore',?,'',?)", (op_id, table, uid, json.dumps(payload), int(time.time()*1000)))
        conn.execute("UPDATE kanban_sync_omissions SET restore_op_id=?,state='restoring' "
                     "WHERE table_name=? AND row_uid=? AND field=? AND state!='delivered'", (op_id, table, uid, field))


def acknowledge(conn, op_id, authoritative, *, sent=None):
    """Release only fields whose actual delivery matches their current local value."""
    import json
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_sync_rejections import _local
    op = conn.execute("SELECT * FROM kanban_sync_outbox WHERE op_id=?", (op_id,)).fetchone()
    if op is None or not isinstance(authoritative, dict):
        return
    row = _local(conn, op['table_name'], op['row_uid'])
    current = client._row_fields(conn, op['table_name'], row) if row else {}
    sent = json.loads(op['fields']) if sent is None else sent
    for field in held_fields(conn, op['table_name'], op['row_uid']) & sent.keys():
        value = sent[field]
        if field not in authoritative or authoritative[field] != value or field not in current:
            continue
        if current[field] == value:
            conn.execute("UPDATE kanban_sync_omissions SET state='delivered' "
                         "WHERE table_name=? AND row_uid=? AND field=?",
                         (op['table_name'], op['row_uid'], field))
        else:
            # Proven delivery of an older local value permits one attempt at the newer value.
            # A losing merge or a missing row must never reset the bound.
            conn.execute("UPDATE kanban_sync_omissions SET state='held',restore_op_id=NULL "
                         "WHERE restore_op_id=? AND field=? AND state!='delivered'", (op_id, field))
            _queue_restore(conn, op['table_name'], op['row_uid'], authoritative, {field})


def remap(conn, old_id, new_id):
    """Follow the existing bounded transient retry's fresh Board verdict identity."""
    for column in ('compat_op_id', 'restore_op_id'):
        conn.execute(f"UPDATE kanban_sync_omissions SET {column}=? WHERE {column}=?", (new_id, old_id))


def repair_holds(conn, table):
    """An omission owns only its field; unrelated or failed compatibility ops still hold the row."""
    return {r[0] for r in conn.execute(
        "SELECT row_uid FROM kanban_sync_outbox o WHERE table_name=? AND state IN ('pending','rejected') "
        "AND NOT (state='rejected' AND EXISTS (SELECT 1 FROM kanban_sync_omissions m "
        "WHERE m.original_op_id=o.op_id AND m.table_name=o.table_name AND m.row_uid=o.row_uid))", (table,))}
