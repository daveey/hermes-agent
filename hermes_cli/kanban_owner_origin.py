"""Local custody issued by the producer-owner attachment, never by link lookup."""
from __future__ import annotations

import json

from hermes_cli import kanban_origin_native as native
from hermes_cli.kanban_origin_ledger import validate_binding


def record(conn, task_id: str, sid: str) -> None:
    """Internal to the trusted ensure+attach path, in its board write transaction.

    The producer has just ensured its registered local owner session. Plain link
    writers, replayed events and replica readers must never invoke this issuer.
    Existing native custody is immutable; attaching a producer cannot replace it.
    """
    scope = native._primary_scope(conn)
    if scope is None or native._receipt(conn, task_id) is not None:
        return
    gateway, profile, board = scope
    row = conn.execute("SELECT created_at FROM tasks WHERE id=? AND "
                       "(created_on IS NULL OR created_on=?)", (task_id, gateway)).fetchone()
    if row is None:
        return
    if not conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=? AND chat_gateway=? "
                        "AND COALESCE(chat_profile,'default')=? AND chat_session=? "
                        "AND kind='created' AND released=0", (task_id, gateway, profile, sid)).fetchone():
        return
    binding = {"binding_id": "native-" + task_id, "version": 1, "publication_version": 1,
               "origin": {"gateway": gateway, "profile": profile, "session": sid},
               "owner": {"gateway": gateway, "profile": profile, "board": board, "task_id": task_id},
               "task_record_key": f"{profile}/{board}/{task_id}"}
    validate_binding(binding)
    native._install_revocation(conn)
    conn.execute("INSERT INTO origin_native_receipts VALUES (?,?,NULL,?)",
                 (task_id, json.dumps(binding, sort_keys=True), row[0]))
