"""Machine-local creation attestations; never reconstructed from synced rows.

The tool captures interactive context, and create_task records it in its own
transaction. An inherited receipt is read only from that same primary board.
"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from hermes_cli.kanban_origin_ledger import validate_binding
from agent.origin_observation_budget import bounded_record, remaining


def capture(creator_task_id, *, courier=False):
    """Use admitted in-process custody, never environment or tool arguments."""
    if courier:
        return None
    if creator_task_id:
        from agent.origin_worker_custody import capture as capture_worker
        return capture_worker(creator_task_id)
    from agent.origin_turn_custody import CapturedOrigin, origin
    qualified = origin()
    return CapturedOrigin(qualified) if qualified else None


def _primary_scope(conn):
    """Require the canonical board file and a current local primary identity."""
    from pathlib import Path
    from hermes_cli.kanban_db import board_own_db_path, kanban_home
    from hermes_cli.kanban_db_sync import _db_key, sync_config, board_selected
    from hermes_cli.kanban_execution_observations import board_owner_profile
    from hermes_cli.kanban_placement_forward import opened_board
    from hermes_cli.kanban_origin import gateway_names
    key = _db_key(conn)
    if not key:
        return None
    board, gateway = opened_board(conn), gateway_names()[0]
    if not board or not gateway:
        return None
    expected = board_own_db_path(board)
    # Resolve the profile home, not a board/DB symlink into another store.
    expected = kanban_home().resolve() / expected.relative_to(kanban_home())
    if Path(key) != expected or expected.resolve() != expected:
        return None
    config = sync_config()
    if not config.get("enabled") or not board_selected(key, config):
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_sync_state'").fetchone():
        return None
    row = conn.execute("SELECT board_id, gateway_id, mode FROM kanban_sync_state").fetchall()
    if len(row) != 1 or tuple(row[0]) != (board, gateway, "primary"):
        return None
    profile = board_owner_profile(expected)
    return (gateway, profile, board) if profile else None


def _install_revocation(conn):
    """Local triggers also cover sync deletes and SQLite REPLACE with recursion off."""
    conn.execute("CREATE TABLE IF NOT EXISTS origin_native_receipts (task_id TEXT PRIMARY KEY, "
                 "binding_json TEXT NOT NULL, inherited_from TEXT, created_at INTEGER NOT NULL)")
    for name, event, key, condition in (
        ("delete", "DELETE", "OLD.id", ""),
        ("insert", "INSERT", "NEW.id", ""),
        ("destination", "UPDATE OF id", "NEW.id", "WHEN OLD.id IS NOT NEW.id"),
        ("identity", "UPDATE OF id, created_at, created_on", "OLD.id",
         "WHEN OLD.id IS NOT NEW.id OR OLD.created_at IS NOT NEW.created_at "
         "OR (OLD.created_on IS NOT NEW.created_on AND NOT (OLD.created_on IS NULL "
         "AND NEW.created_on IS (SELECT json_extract(binding_json, '$.owner.gateway') "
         "FROM origin_native_receipts WHERE task_id=OLD.id)))"),
    ):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS origin_native_revoke_{name} AFTER {event} "
                     f"ON tasks {condition} BEGIN DELETE FROM origin_native_receipts WHERE task_id={key}; END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS origin_native_revoke_store_update "
                 "AFTER UPDATE OF board_id, gateway_id, mode ON kanban_sync_state "
                 "WHEN OLD.board_id IS NOT NEW.board_id OR OLD.gateway_id IS NOT NEW.gateway_id "
                 "OR OLD.mode IS NOT NEW.mode BEGIN DELETE FROM origin_native_receipts; END")
    for event in ("INSERT", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS origin_native_revoke_store_{event.lower()} "
                     f"AFTER {event} ON kanban_sync_state BEGIN DELETE FROM origin_native_receipts; END")


def _receipt(conn, task_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_native_receipts'").fetchone():
        return None
    row = conn.execute("SELECT binding_json FROM origin_native_receipts WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    binding = json.loads(row[0])
    validate_binding(binding)
    if not conn.execute("SELECT 1 FROM tasks WHERE id=? AND (created_on IS NULL OR created_on=?)",
                        (task_id, binding["owner"]["gateway"])).fetchone():
        return None
    return binding


@bounded_record
def record(conn, task_id, context, created_at):
    """Called inside create_task's transaction; no event or sync projection."""
    from agent.origin_turn_custody import CapturedOrigin
    from agent.origin_worker_custody import CapturedWorkerOrigin
    if type(context) not in (CapturedOrigin, CapturedWorkerOrigin):
        return
    captured = context
    context = captured.validated(conn) if type(captured) is CapturedWorkerOrigin else captured.validated()
    if context is None:
        return
    scope = _primary_scope(conn)
    if scope is None:
        return
    gateway, profile, board = scope
    if not conn.execute("SELECT 1 FROM tasks WHERE id=? AND (created_on IS NULL OR created_on=?)", (task_id, gateway)).fetchone():
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
    _install_revocation(conn)
    checked = captured.validated(conn) if type(captured) is CapturedWorkerOrigin else captured.validated()
    if checked != context:
        return
    remaining()
    conn.execute("INSERT INTO origin_native_receipts VALUES (?,?,?,?)",
                 (task_id, json.dumps(binding, sort_keys=True), creator, created_at))


def record_creation(conn, task_id, captured, created_at):
    """Commit a captured native source and its forwarding custody atomically.

    Recording stays bounded and revalidates authority itself. A declined record
    must roll back creation, not dispatch an unforwardable source. Legacy callers
    without a captured capability, unsynced boards and courier intents acquire
    no authority from this check. Freeze eligibility before recording so losing
    primary scope during the observation cannot silently downgrade admission.
    """
    from agent.origin_turn_custody import CapturedOrigin
    scope = _primary_scope(conn) if type(captured) is CapturedOrigin else None
    record(conn, task_id, captured, created_at)
    if scope is None:
        return
    require_created_receipt(conn, task_id, captured, scope=scope)


def require_created_receipt(conn, task_id, captured, *, scope=None):
    """Refuse silent legacy replay as well as a declined new record; never adopt."""
    from agent.origin_turn_custody import CapturedOrigin
    from hermes_cli.kanban_origin_admission import AdmissionRefused
    if type(captured) is not CapturedOrigin:
        return
    scope = scope or _primary_scope(conn)
    if scope is None:
        return
    binding = _receipt(conn, task_id)
    gateway, profile, board = scope
    owner = {'gateway': gateway, 'profile': profile, 'board': board, 'task_id': task_id}
    if (_primary_scope(conn) != scope or binding is None
            or binding['origin'] != captured['origin'] or binding['owner'] != owner):
        raise AdmissionRefused("native receipt unavailable; no source admitted; retry the live owning turn or use supported origin adoption")


def lookup(origin, creator, *, gateway):
    """Read only the qualified local primary board; never a shadow replica."""
    if creator["gateway"] != gateway:
        return None
    from hermes_cli.kanban_db import board_own_db_path
    from hermes_cli.kanban_execution_observations import board_owner_profile
    from hermes_cli import kanban_lockguard
    path = board_own_db_path(creator["board"])
    if not path.exists() or board_owner_profile(path) != creator["profile"]:
        return None
    with closing(kanban_lockguard.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        if _primary_scope(conn) != (gateway, creator["profile"], creator["board"]):
            return None
        binding = _receipt(conn, creator["task_id"])
    if binding and binding["owner"] == creator and binding["origin"] == origin:
        return binding
    return None
