"""Opt-in creation observations; SQLite, not a memory queue, owns commit custody."""
from __future__ import annotations

import sqlite3

from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli.kanban_fairness_payloads import creation_payload
from hermes_cli.kanban_priority import PRIORITY_CAP


def _binding(conn, board):
    if not runtime.active_boards():
        return board, None
    if not board:
        from hermes_cli.kanban_db_sync import _db_key, board_slug_for_path

        board = board_slug_for_path(_db_key(conn) or "")
    return board, runtime.capture_id(board=board)


def observe_intent(conn, args, creator, receipt, *, effective=None):
    """Observe original tool intent, never expose internal evidence as courier arguments."""
    if receipt is None:
        return
    board, capture = _binding(conn, args.get("board"))
    if capture is None:
        return
    if effective is None and creator is not None:
        from hermes_cli.kanban_admission_score import effective_priorities

        try:
            effective = effective_priorities(conn, [creator])
        except (sqlite3.Error, ValueError, TypeError):
            runtime.invalidate(board=board)
            return
    receipt.update(capture_id=capture, creator_task_uid=creator,
                   priority_intent="explicit" if "priority" in args else "omitted",
                   priority_argument=args.get("priority"),
                   effective_parent_priority=(effective or {}).get(creator))


def _payload(conn, task_id, creator, receipt):
    receipt = receipt or {}
    # No authoritative ready-entry cut is carried by this caller. A local run integer is not an episode UID.
    payload = dict(task_uid=task_id, creator_task_uid=creator, creator_episode_uid=None,
                   priority_intent=receipt.get("priority_intent", "unknown"),
                   priority_argument=receipt.get("priority_argument"),
                   effective_parent_priority=receipt.get("effective_parent_priority"),
                   parent_snapshot_cut=None, inheritance_cap=PRIORITY_CAP,
                   stored_child_priority=conn.execute("SELECT priority FROM tasks WHERE id=?", (task_id,)).fetchone()[0],
                   producer_source_hash=None)
    payload["unknown_fields"] = [key for key, value in payload.items() if value is None
                                and not (key == "priority_argument" and payload["priority_intent"] != "unknown")]
    if payload["priority_intent"] == "unknown":
        payload["unknown_fields"].append("priority_intent")
    return creation_payload(payload)


def _append(conn, task_id, capture, payload):
    """Use the journal's existing bounds and rollback domain, without executescript's implicit commit."""
    if not journal._has(conn, journal.SESSION):
        return False
    row = conn.execute(f"SELECT capture_id, sealed_ms FROM {journal.SESSION} WHERE id=1").fetchone()
    if row is None or row[0] != capture or row[1] is not None:
        return False
    fields = runtime.bounded_payload(payload).decode("utf-8")
    # The SQL is fixed journal-owned text; all observed values are bound, never interpolated.
    for statement in journal._append("creation", ":task", None, ":fields").split(";"):
        if statement.strip():
            conn.execute(statement, {"task": task_id, "fields": fields})
    return True


def stage(conn, task_id, *, creator_task_id, board, receipt):
    board, capture = _binding(conn, board)
    if capture is None:
        return
    try:
        if receipt and (receipt.get("capture_id") != capture or receipt.get("creator_task_uid") != creator_task_id):
            runtime.invalidate(board=board)
            return
        payload = _payload(conn, task_id, creator_task_id, receipt)
        if not _append(conn, task_id, capture, payload):
            runtime.invalidate(board=board)
    except (sqlite3.Error, ValueError, TypeError):
        # Observation failure makes the capture incomplete, never changes scheduling policy.
        runtime.invalidate(board=board)
