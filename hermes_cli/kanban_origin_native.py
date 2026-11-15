"""Machine-local creation attestations; never reconstructed from synced rows.

The tool captures interactive context, and create_task records it in its own
transaction. An inherited receipt is read only from that same primary board.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3

from hermes_cli.kanban_origin_ledger import validate_binding


def capture(creator_task_id, *, courier=False):
    """Trusted ambient context, never the tool's session/origin arguments."""
    if courier:
        return None
    worker = os.environ.get("HERMES_KANBAN_TASK")
    if worker:
        return {"creator": worker} if creator_task_id == worker else None
    if creator_task_id:
        return None
    from gateway.session_context import get_session_env
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.kanban_session_provenance import _local_session_sources, _INTERACTIVE_SOURCES
    from hermes_cli.kanban_placement import is_placeholder_session
    sid = get_session_env("HERMES_SESSION_ID")
    gateway, profile = gateway_names()[0], get_active_profile_name()
    if (not gateway or profile == "custom" or not sid or is_placeholder_session(sid)
            or _local_session_sources([sid]).get(sid) not in _INTERACTIVE_SOURCES):
        return None
    return {"origin": {"gateway": gateway, "profile": profile, "session": sid}}


def _receipt(conn, task_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_native_receipts'").fetchone():
        return None
    row = conn.execute("SELECT binding_json FROM origin_native_receipts WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    binding = json.loads(row[0])
    validate_binding(binding)
    return binding


def record(conn, task_id, context, created_at):
    """Called inside create_task's transaction; no event or sync projection."""
    if context is None:
        return
    from hermes_cli.kanban_db_sync import _db_key
    from hermes_cli.kanban_execution_observations import board_owner_profile
    from hermes_cli.kanban_placement_forward import opened_board
    from hermes_cli.kanban_origin import gateway_names
    gateway = gateway_names()[0]
    profile = board_owner_profile(_db_key(conn))
    board = opened_board(conn)
    if not all((gateway, profile, board)):
        return
    creator = context.get("creator")
    if creator:
        parent = _receipt(conn, creator)
        expected = {"gateway": gateway, "profile": profile, "board": board, "task_id": creator}
        if parent is None or parent["owner"] != expected:
            return
        origin = parent["origin"]
    else:
        origin = context["origin"]
    if origin["gateway"] != gateway:
        return
    binding = {"binding_id": "native-" + task_id, "version": 1, "publication_version": 1,
               "origin": origin, "owner": {"gateway": gateway, "profile": profile, "board": board,
                                           "task_id": task_id},
               "task_record_key": f"{profile}/{board}/{task_id}"}
    validate_binding(binding)
    conn.execute("CREATE TABLE IF NOT EXISTS origin_native_receipts (task_id TEXT PRIMARY KEY, "
                 "binding_json TEXT NOT NULL, inherited_from TEXT, created_at INTEGER NOT NULL)")
    conn.execute("INSERT INTO origin_native_receipts VALUES (?,?,?,?)",
                 (task_id, json.dumps(binding, sort_keys=True), creator, created_at))


def lookup(origin, creator, *, gateway):
    """Read only the qualified local primary board; never a shadow replica."""
    if creator["gateway"] != gateway:
        return None
    from hermes_cli.kanban_db import kanban_db_path
    from hermes_cli.kanban_execution_observations import board_owner_profile
    from hermes_cli import kanban_lockguard
    path = kanban_db_path(creator["board"])
    if not path.exists() or board_owner_profile(path) != creator["profile"]:
        return None
    with closing(kanban_lockguard.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        from hermes_cli.kanban_placement_forward import opened_board
        if opened_board(conn) != creator["board"]:
            return None
        binding = _receipt(conn, creator["task_id"])
    if binding and binding["owner"] == creator and binding["origin"] == origin:
        return binding
    return None
