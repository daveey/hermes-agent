"""Read-only full-replica custody for absent, foreign-origin link endpoints.

The replica is evidence, never a source of task imports into the live partition.
Reads are transaction-bound; no network call occurs in either database's txn.
"""
from __future__ import annotations


def snapshot(board, gateway, uid, local):
    from hermes_cli import kanban_sync_client as client
    from hermes_cli import kanban_sync_dependency_delete as recovery
    from hermes_cli.kanban_containment_connections import held
    from hermes_cli.kanban_db_sync import sync_state

    absent = sorted(set(recovery._ids(uid)) - {t["id"] for t in local["tasks"]})
    if not absent:
        return None
    shadow = client.open_shadow(board, gateway)
    try:
        shadow.execute("BEGIN")
        state = sync_state(shadow)
        tasks = [dict(row) for task in absent for row in shadow.execute("SELECT * FROM tasks WHERE id=?", (task,))]
        links = recovery._rows(shadow, "SELECT * FROM task_links WHERE parent_id=? AND child_id=?", recovery._ids(uid))
        return {"cursor": state["cursor"], "tasks": tasks, "held": [held(shadow, t) for t in absent], "link": links}
    finally:
        if shadow.in_transaction:
            shadow.rollback()
        shadow.close()


def validate(row, replica, gateway, context):
    from hermes_cli.kanban_sync_dependency_delete import _require
    from hermes_cli.kanban_containment_policy import identity

    origin = row.get("created_on")
    # A missing own-origin (or legacy origin-less) endpoint is still missing.
    _require(isinstance(origin, str) and bool(origin) and origin != gateway, "missing_endpoint")
    identity({"board": context["board"], "gateway": origin, "task_id": row.get("id")})
    copies = [t for t in replica["tasks"] if t["id"] == row.get("id")]
    _require(len(copies) == 1, "missing_endpoint_replica")
    task = copies[0]
    _require(task["created_on"] == origin and type(task["sync_version"]) is int
             and task["sync_version"] == row["version"], "endpoint_identity_changed")
    _require(context["target"]["gateway"] == origin, "endpoint_context_changed")
    _require(not any(replica["held"]), "replica_containment_hold")
    _require(all(task.get(key) == row.get(key) for key in
                 ("status", "lease_holder", "lease_expires", "containment_episode")), "endpoint_replica_changed")


def recheck(board, gateway, uid, local, proof):
    from hermes_cli.kanban_sync_dependency_delete import _require

    if "foreign" in proof:
        _require(snapshot(board, gateway, uid, local) == proof["foreign"], "replica_inputs_changed")
