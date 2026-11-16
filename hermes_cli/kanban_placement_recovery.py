"""Current-input recovery eligibility; the Board alone changes assignments."""
from __future__ import annotations

import json
import math
import time

RECOVERY_RETRY_S = 600.0
_unsupported: dict[tuple, float] = {}
_scan_cursor: dict[tuple, str] = {}


def select_rows(conn, board, gateway, cursor, limit):
    """Give recovery its own bounded scan; legacy exclusions cannot starve renewals.

    Reservation priority and its durable cursor remain unchanged. The extra recovery
    lane shares the caller's network deadline and rotates within this process.
    """
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_worker_placement import POLICY

    query = (
        "SELECT * FROM tasks WHERE placement_policy IS ? AND status='ready' "
        "AND created_on=? AND claim_lock IS NULL AND lease_holder IS NULL AND sync_version IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM kanban_sync_outbox o WHERE o.table_name='tasks' "
        "AND o.row_uid=tasks.id AND o.state IN ('pending','rejected')) ORDER BY (id <= ?), id LIMIT ?")
    policy = conn.execute(query, (POLICY, gateway, cursor, limit)).fetchall()
    key = (_main_db_file(conn), board, gateway)
    legacy = conn.execute(query, (None, gateway, _scan_cursor.get(key, ""), limit)).fetchall()
    if legacy:
        _scan_cursor[key] = legacy[-1]["id"]
        if len(_scan_cursor) > 1024:
            del _scan_cursor[next(iter(_scan_cursor))]
    return policy + legacy


def endpoint_key(hub, board, gateway):
    ident = getattr(hub, "ident", None)
    return (ident.url if ident is not None else hub, board, gateway)


def endpoint_available(hub, board, gateway):
    return time.monotonic() >= _unsupported.get(endpoint_key(hub, board, gateway), 0)


def unavailable_endpoint(hub, board, gateway, error, mode):
    """Bound old-server probes across cards without disabling ordinary reserves."""
    if not mode or error.status not in (404, 405, 501):
        return False
    _unsupported[endpoint_key(hub, board, gateway)] = time.monotonic() + RECOVERY_RETRY_S
    if len(_unsupported) > 1024:
        del _unsupported[next(iter(_unsupported))]
    return True


def available_mode(conn, row, cfg, hub, board, gateway):
    # Existing policy cards still renew through reserve while recovery is unavailable.
    return recovery_mode(conn, row, cfg, board=board) if endpoint_available(hub, board, gateway) else None


def request_assignment(hub, board, row, mode, candidates):
    """Keep recovery opt-in; ordinary reservations retain their existing wire contract."""
    args = {"task_id": row["id"], "expected_revision": row["sync_version"], "candidates": candidates}
    return hub.recover(board, mode=mode, **args) if mode else hub.reserve(board, **args)


def unchanged_ready(conn, row):
    """Recheck the selected snapshot after preceding network calls, without a network-spanning lock."""
    current = conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
    if current is None or dict(current) != dict(row):
        return False
    return not conn.execute(
        "SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? "
        "AND state IN ('pending','rejected') LIMIT 1", (row["id"],)).fetchone()


def recovery_mode(conn, row, cfg, *, board="default"):
    """Propose a stale-pin recovery only from an affirmative registry observation."""
    from hermes_cli import kanban_worker_node as node
    from hermes_cli.kanban_worker_placement import requested_move

    if row.get("placement_policy") and requested_move(conn, row["id"]) is not None:
        return None  # an unapplied owner move takes precedence over automatic recovery
    if row.get("runs_on") is None:
        return promotion_mode(conn, row, cfg, board=board) if row.get("placement_policy") is None else None
    try:
        labels = node.normalize_runs_on(row["runs_on"])
    except (TypeError, ValueError):
        return None
    names = [label.removeprefix("machine:") for label in labels or [] if label.startswith("machine:")]
    if len(names) != 1:
        return None
    snapshot = node.registry(require_fresh=True)
    machines = snapshot.get("machines") if isinstance(snapshot, dict) else None
    if not isinstance(machines, dict):
        return None
    if names[0] not in machines:
        # Active-list discovery omits revoked names. Only the Board can distinguish
        # a retired pin from an unknown name; absence is a proposal, not authority.
        return "stale_pin" if current_portable(conn, row, cfg) else None
    entry = machines[names[0]]
    if not isinstance(entry, dict):
        return None
    seen = entry.get("last_seen")
    stale = (type(seen) in (int, float) and math.isfinite(seen) and seen > 0
             and time.time() * 1000 - seen > 30 * 60 * 1000)
    if entry.get("state") not in ("revoked", "inactive") and not entry.get("hold") and not stale:
        return None
    return "stale_pin" if current_portable(conn, row, cfg) else None


def current_portable(conn, row, cfg):
    from hermes_cli.kanban_placement import portable_reason

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='origin_creation_intents'").fetchone():
        if conn.execute("SELECT 1 FROM origin_creation_intents WHERE task_id=? LIMIT 1", (row["id"],)).fetchone():
            return False  # courier binding owns the destination, including historical creates
    # Even scratch work can contain unpublished outputs from an earlier run.
    if row.get("workspace_path") or conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (row["id"],)).fetchone():
        return False
    args = dict(row)
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                         "ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    try:
        created = json.loads(event[0]) if event else None
        if not isinstance(created, dict):
            return False
        # Allocated scratch path is not an input, but declared paths/projects remain constraints.
        args["workspace_path"] = created.get("workspace_path")
        args["project_id"] = row.get("project_id") or created.get("project_id")
        args["board"] = "default"
        args["runs_on"] = None  # Board preserves/rechecks the replacement's other labels.
        from hermes_cli.kanban_mandatory_pin import worker_first_refusal
        if worker_first_refusal(args):
            return False  # recovery assigns through the worker-first lease, which cannot demand the pin label
        for key in ("requires", "skills"):
            args[key] = json.loads(args[key]) if isinstance(args.get(key), str) else args.get(key)
            if args[key] is not None and (not isinstance(args[key], list)
                                         or not all(isinstance(v, str) for v in args[key])):
                return False
        return portable_reason(args, cfg) is None
    except (TypeError, ValueError):
        return False


def promotion_mode(conn, row, cfg, *, board="default"):
    """Legacy local routing was not a machine pin; preserve explicit durable intent.

    An old policy cleared by an owner repin must never be silently re-enrolled.
    Created portability is historical diagnostics, not today's eligibility decision.
    """
    if row.get("runs_on") is not None:
        return None
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                         "ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    if event is None:
        return None
    try:
        created = json.loads(event[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(created, dict) or created.get("placement_intent") == "explicit" or created.get("placement_policy"):
        return None
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                    "('moved','placement_repinned') LIMIT 1", (row["id"],)).fetchone():
        return None
    if not current_portable(conn, row, cfg):
        return None
    return None if hub_keep_reason(conn, row, cfg, board=board) else "promote"


def hub_keep_reason(conn, row, cfg, *, board="default"):
    """The hub claim screen's reason to keep this card here (``kanban_hub_admission_claim.keep_reason``: a
    hub-local wake_check, a hub worktree in the brief, host_repo, ...), or None.

    Promotion must not move a card the claim screen would keep: whichever of the two reaches a legacy-routed hub
    card first decides it, so both read the same rules. An unreadable card keeps it (no promotion).
    ``board`` is the reevaluated board (only "default" today: ``reevaluate`` returns early for any other).
    """
    from hermes_cli.kanban_hub_admission_claim import keep_reason
    from hermes_cli.kanban_hub_admission_pickup import card_fields
    try:
        return keep_reason(conn, row, card_fields(row, board), cfg)
    except (LookupError, TypeError, ValueError):  # LookupError: KeyError (dict row) or IndexError (sqlite3.Row)
        return "card fields unreadable"
